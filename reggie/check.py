# ==================================================================================================================================
# Copyright (c) 2017 - 2018 Stephen Copplestone and Matthias Sonntag
#
# This file is part of reggie (github.com/reggie-framework/reggie). reggie is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version.
#
# reggie is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License v3.0 for more details.
#
# You should have received a copy of the GNU General Public License along with reggie. If not, see <http://www.gnu.org/licenses/>.
# ==================================================================================================================================
import os
import sys
import re
import shutil
import subprocess
import tempfile
import threading
import io
from functools import partial
from multiprocessing.pool import ThreadPool

from reggie import combinations
from reggie import tools
from reggie import summary
from reggie.analysis import Analyze, getAnalyzes, Clean_up_files, Analyze_compare_across_commands
from reggie.outputdirectory import OutputDirectory
from reggie.externalcommand import ExternalCommand
from reggie.args_parser import getMaxCPUCores

# import h5 I/O routines
try:
    import h5py

    h5py_module_loaded = True
except ImportError:
    h5py_module_loaded = False


class Build(OutputDirectory, ExternalCommand):
    def __init__(self, basedir, source_directory, configuration, number, name='build', binary_path=None, coverage=None):
        # fmt: off
        self.basedir          = basedir
        self.source_directory = source_directory
        self.configuration    = configuration
        self.MPIbuilt         = False   # serial built as default
        OutputDirectory.__init__(self, None, name, number)
        ExternalCommand.__init__(self)
        # fmt: on

        # initialize result as empty list
        self.result = tools.yellow("skipped building")

        # initialize examples as empty list
        self.examples = []

        # set path to binary/executable
        if binary_path :  # fmt: skip
            self.binary_path = binary_path
            head, tail = os.path.split(binary_path)
            self.binary_dir = head
            binary_name = tail
        else:
            # get 'binary' from 'configuration' dict and remove it
            try:
                binary_name = self.configuration["binary"]
            except Exception:
                print(tools.red("No 'binary'-option with the name of the binary specified in 'builds.ini'"))
                sys.exit(1)
            self.configuration.pop('binary', None)  # remove binary from config dict
            self.binary_dir = os.path.abspath(os.path.join(self.target_directory))
            self.binary_path = os.path.abspath(os.path.join(self.target_directory, binary_name))

        # set cmake command
        self.cmake_cmd = ["cmake"]  # start composing cmake command
        self.cmake_cmd_color = ["cmake"]  # start composing cmake command with colors
        for key, value in self.configuration.items():  # add configuration to the cmake command
            self.cmake_cmd.append(f"-D{key}={value}")
            self.cmake_cmd_color.append(tools.blue("-D") + f"{key}={value}")

        # add compiler options to each combination for code coverage
        coverage_env = os.getenv('CODE_COVERAGE')
        if coverage_env or coverage:
            coverage_flags = '"--coverage"'
            self.cmake_cmd.append('-DCMAKE_Fortran_FLAGS=' + coverage_flags)
            self.cmake_cmd_color.append(tools.blue("-D") + "CMAKE_Fortran_FLAGS=" + f'{coverage_flags}')

        self.cmake_cmd.append(self.basedir)  # add basedir to the cmake command
        self.cmake_cmd_color.append(self.basedir)  # add basedir to the cmake command

    def compile(self, buildprocs):
        # don't compile if build directory already exists
        if self.binary_exists():  # if the binary exists, return
            print("skipping")
            return
        # for build carryon: when a binary is missing remove all examples (re-run all examples)
        print("removing folder, ", end=' ')  # skip linebreak
        shutil.rmtree(self.target_directory, ignore_errors=True)
        os.makedirs(self.target_directory)
        tools.create_folder(self.target_directory)
        print("building")

        # CMAKE: execute cmd in build directory
        # fmt: off
        s_Color   = "C-making with [{}] ...".format(" ".join(self.cmake_cmd_color))
        s_NoColor = "C-making with [{}] ...".format(" ".join(self.cmake_cmd))
        # fmt: on

        if self.execute_cmd(self.cmake_cmd, self.target_directory, string_info=s_Color) != 0:  # use uncolored string for cmake
            raise BuildFailedException(self)  # "CMAKE failed"

        # MAKE: default with '-j'
        if not os.path.exists(os.path.join(self.target_directory, "build.ninja")):
            self.make_cmd = ["make", "-j"]
            if buildprocs > 0:
                self.make_cmd.append(str(buildprocs))
        else:
            self.make_cmd = ["ninja"]
            if buildprocs == 0:
                self.make_cmd.append("-j0")
            elif buildprocs > 0:
                self.make_cmd.append("-j" + str(buildprocs))
        # execute cmd in build directory
        s_NoColor = "Building with [{}] ...".format(" ".join(self.make_cmd))

        if self.execute_cmd(self.make_cmd, self.target_directory, string_info=s_NoColor) != 0:
            raise BuildFailedException(self)  # "MAKE failed"
        print('-' * 132)

    def __str__(self):
        return "BUILD in: " + self.target_directory

    def binary_exists(self):
        return os.path.exists(self.binary_path)


class Standalone(Build):
    def __init__(self, binary_path, source_directory):
        Build.__init__(self, None, source_directory, {}, -1, "standalone", os.path.abspath(binary_path))

    def compile(self, buildprocs):
        pass

    def __str__(self):
        s = "standalone :       binary_path= " + self.binary_path + "\n"
        s += "              target_directory= " + self.target_directory
        return s


def StandaloneAutomaticMPIDetection(binary_path):
    """Try and find CMake option specifying if the executable was built with MPI=ON or without any MPI libs"""
    # Default (per definition)
    MPIifOFF = False
    userblockChecked = False

    # 1st Test
    # Use try/except here, but don't terminate the program when try fails
    try:
        # Check if userblock exists and read it, otherwise don't do anything and continue
        userblock = os.path.join(os.path.dirname(os.path.abspath(binary_path)), 'userblock.txt')
        # print("Checking userblock under %s " % userblock)
        if os.path.exists(userblock):
            checkCMAKELine = False
            checklibstaticLine = False
            with open(userblock) as f:
                for line in f:  # iterate over all lines of the file
                    line = line.rstrip('\n')

                    # Only check lines within the "{[( CMAKE )]}" block
                    if checkCMAKELine:
                        Parentheses = re.search(r'\((.+)\)', line)
                        if Parentheses:
                            # fmt: off
                            text = Parentheses.group(0)       # get text
                            text = text[1:-1]                 # remove opening and closing parentheses
                            text = re.sub(r'".*"', '', text)  # remove double quotes and their content
                            # fmt: on
                            parameters = text.split()
                            MPI_built_flags = [os.path.basename(binary_path).upper() + "_MPI", 'LIBS_USE_MPI']
                            if any(parameters[0] == flag for flag in MPI_built_flags):
                                value = parameters[len(parameters) - 1]
                                if value.lower() == 'off':
                                    MPIifOFF = True
                                    userblockChecked = True
                                    print(tools.yellow(f"Automatically determined that the executable was compiled with MPI=OFF\n  File: {userblock}\n  Line: {line}"))
                                    break
                                if value.lower() == 'on':
                                    MPIifOFF = False
                                    userblockChecked = True
                                    print(tools.yellow(f"Automatically determined that the executable was compiled with MPI=ON\n  File: {userblock}\n  Line: {line}"))
                                    break

                    # Only check lines within the "{[( libpiclasstatic.dir/flags.make )]}" block
                    if checklibstaticLine:
                        if "-DUSE_MPI=0" in line:
                            MPIifOFF = True
                            userblockChecked = True
                            print(tools.yellow(f"Automatically determined that the executable was compiled with MPI=OFF (-DUSE_MPI=0)\n  File: {userblock}\n  Line: {line}"))
                            break
                        if "-DUSE_MPI=1" in line:
                            MPIifOFF = False
                            userblockChecked = True
                            print(tools.yellow(f"Automatically determined that the executable was compiled with MPI=ON (-DUSE_MPI=1)\n  File: {userblock}\n  Line: {line}"))
                            break

                    # Check which block is being passed and extract the "CMAKE" block, other blocks will be ignores
                    # Find { } strings
                    Braces = re.search(r'\{(.+)\}', line)
                    if Braces:
                        # mytext = line
                        mytext = Braces.group(0)
                        # Find [ ] strings
                        SquareBrackets = re.search(r'\[(.+)\]', mytext)
                        # Check string within [ ]
                        if SquareBrackets:
                            # Get string
                            mytext = SquareBrackets.group(0)
                            # Find ( ) strings
                            Parentheses = re.search(r'\((.+)\)', mytext)
                            # Check string within ( )
                            if Parentheses:
                                # Get string
                                parameter = Parentheses.group(0)  # get text
                                # Remove opening and closing parentheses
                                parameter = parameter[1:-1]
                                # Remove leading and trailing white spaces
                                parameter = parameter.strip()
                                # Get lower case string and set logical if, finally, the line {[( CMAKE )]} is found
                                checkCMAKELine = parameter.lower() == 'cmake'
                                # Get lower case string and set logical if, finally, the line {[( libpiclasstatic.dir/flags.make )]} is found
                                parameter = parameter.lower()
                                checklibstaticLine = bool(parameter.startswith('lib') and parameter.endswith('static.dir/flags.make'))

    except Exception as e:
        print(tools.red(f"Error checking userblock in StandaloneAutomaticMPIDetection() in check.py:\nError message [{e}]\nThis program, however, will not be terminated!"))

    # 2nd Test
    # If the userblock test did not result in MPIifOFF=True, check the shared object dependencies of the executable and search for MPI related libs
    # Note that this is not fully accurate, as the executable might be compiled with MPI=OFF but with the MPI libs loaded and therefore still
    # present in ldd (maybe add info to piclas/flexi/... --help stating if it was compiled single-core or multi-core)
    if not MPIifOFF and not userblockChecked:
        # Use try/except here, but don't terminate the program when try fails
        try:
            cmd = ['ldd', binary_path, '|', 'grep', '-i', r'"libmpi\.\|\<libmpi_"']
            a = ' '.join(cmd)
            pipe = subprocess.Popen(a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
            (std, err) = pipe.communicate()

            if not isinstance(std, str):
                # convert byte std to string
                std = std.decode("utf-8", 'ignore')

            if not isinstance(err, str):
                # convert byte err to string
                err = err.decode("utf-8", 'ignore')

            # Check if the grep result is not empty
            if std or 'not a dynamic executable' in err:
                MPIifOFF = False
                if 'not a dynamic executable' in err:
                    err = err.rstrip('\n')
                    err = err.lstrip()
                    print(tools.yellow(f"Automatically determined that the executable was compiled with MPI libs (because file is not a dynamic executable)\n  File: {binary_path}\n  Test: {a} -> returned '{err}'"))
                else:
                    print(tools.yellow(f"Automatically determined that the executable was compiled with MPI libs\n  File: {binary_path}\n  Test: {a} -> returned '{std}'"))
            else:
                MPIifOFF = True
                print(tools.yellow(f"Automatically determined that the executable was compiled without MPI libs\n  File: {binary_path}\n  Test: {a} -> returned '{err}'"))

        except Exception as e:  # this fails, if the supplied command line is corrupted
            print(tools.red(f"Error using ldd in StandaloneAutomaticMPIDetection() in check.py:\nError message [{e}]\nThis program, however, will not be terminated!"))

    return MPIifOFF


def getBuilds(basedir, source_directory, CMAKE_BUILD_TYPE, singledir, coverage):
    combis, _ = combinations.getCombinations(os.path.join(source_directory, 'builds.ini'), OverrideOptionKey='CMAKE_BUILD_TYPE', OverrideOptionValue=CMAKE_BUILD_TYPE)

    # create Builds
    return [Build(basedir, source_directory, b, 0, coverage=coverage) for b in combis] if singledir else [Build(basedir, source_directory, b, i, coverage=coverage) for i, b in enumerate(combis, start=1)]


class BuildFailedException(Exception):
    def __init__(self, build):
        self.build = build

    def __str__(self):
        return f"build.compile failed in directory '{self.build.target_directory}'."


# ==================================================================================================


class Example(OutputDirectory):
    def __init__(self, source_directory, build):
        self.source_directory = source_directory
        OutputDirectory.__init__(self, build, os.path.join("examples", os.path.basename(self.source_directory)))

    def __str__(self):
        s = tools.yellow("EXAMPLE in: " + self.source_directory)
        return tools.indent(s, 1)


def getExamples(path, build, log):
    # checks directory with 'builds.ini'
    example_paths = [os.path.join(path, p) for p in sorted(os.listdir(path)) if os.path.isdir(os.path.join(path, p))] if os.path.exists(os.path.join(build.source_directory, 'builds.ini')) else [path]

    examples = []  # list of examples for each build
    # iterate over all example paths (directories of the examples)
    for p in example_paths:
        log.info('-' * 132)
        log.info(tools.blue("example " + str(p)))
        # check if example should be excluded for the build.configuration
        exclude_path = os.path.join(p, 'excludeBuild.ini')
        if os.path.exists(exclude_path):
            log.info(tools.blue("excludes under " + str(exclude_path)))
            # get all keys+values in 'excludeBuild.ini'
            options, _, _ = combinations.readKeyValueFile(exclude_path)
            # list of all excludes for comparison with 'build.configuration'
            excludes = [{option.name: value} for option in options for value in option.values]
            if combinations.anyIsSubset(excludes, build.configuration):
                log.info(tools.red("  skipping example"))
                continue  # any of the excludes matches the build.configuration.
            # Skip this example for the build.configuration
            log.info(tools.yellow("  not skipping"))
        examples.append(Example(p, build))
    return examples


# ==================================================================================================
class Command_Lines(OutputDirectory):
    def __init__(self, parameters, example, number):
        self.parameters = parameters
        OutputDirectory.__init__(self, example, 'cmd', number)

    def __str__(self):
        s = "command_line parameters:\n"
        s += ",".join([f"{k}: {v}" for k, v in self.parameters.items()])
        return tools.indent(s, 2)


def getCommand_Lines(path, example, MPIbuilt, MaxCores):
    command_lines = []
    # If single execution is to be performed, remove "MPI =! 1" from command line list
    if not MPIbuilt:
        combis, _ = combinations.getCombinations(path, OverrideOptionKey='MPI', OverrideOptionValue='1')
    else:
        combis, _ = combinations.getCombinations(path, MaxCores=MaxCores)

    for i, r in enumerate(combis):
        command_lines.append(Command_Lines(r, example, i + 1))

    return command_lines


def getRestartFileList(example):
    options_list, _, _ = combinations.readKeyValueFile(os.path.join(example.source_directory, 'command_line.ini'))
    options = {}  # dict
    for option in options_list:
        # set all upper case characters to lower case
        if len(option.values) > 1:
            options[option.name.lower()] = option.values  # set name to lower case
        else:
            options[option.name.lower()] = option.values[0]  # set name to lower case
        # check for empty lists and abort
        if option.values[0] == '':
            raise Exception(tools.red(f"initialization of analyze.ini failed due to empty parameter [{option.name} = {option.values}], which is not allowed."))

    return options.get('restart_file')


# ==================================================================================================
def SetMPIrun(build, args, MPIthreads):
    """Check MPI built binary (only possible for reggie-compiled binaries)"""
    if MPIthreads:
        # Check if single execution is wanted (independent of the compiled executable)
        if args.noMPI:
            print(tools.indent(tools.yellow(f"noMPI={args.noMPI}, running case in single (without 'mpirun -np')"), 2))
            cmd = []
        elif args.noMPIautomatic:
            print(tools.indent(tools.yellow(f"noMPIautomatic={args.noMPIautomatic}, running case in single (without 'mpirun -np')"), 2))
            cmd = []
        else:
            # Check whether the compiled executable was created with MPI=ON
            if build.MPIbuilt:
                if args.hlrs:
                    cmd = ["aprun", "-n", MPIthreads, "-N", MPIthreads] if int(MPIthreads) < 24 else ["aprun", "-n", MPIthreads, "-N", "24"]
                else:
                    if args.MPIexe == 'mpirun':
                        if args.MaxCores > 0 or args.detectedMPICH:
                            # MPICH core limit due to massive drop in performance when using over-subscription
                            if args.MaxCores > 0 and args.MaxCores < int(MPIthreads):
                                tmpStr = "MPICH" if args.detectedMPICH else "MaxProcs"

                                print(tools.indent(tools.yellow(f"{tmpStr} process limit activated: Setting MPIthreads={args.MaxCores} (originally was {MPIthreads})"), 3))
                                MPIthreads = str(args.MaxCores)
                            cmd = [args.MPIexe, "-np", MPIthreads]
                        else:
                            # Assume OpenMPI
                            cmd = [args.MPIexe, "-np", MPIthreads, "--oversubscribe"]
                    else:
                        # Something else
                        cmd = [args.MPIexe]
            else :  # fmt: skip
                print(tools.indent(tools.yellow(f"Binary has been built with MPI=OFF with external setting MPIthreads={MPIthreads}, running case in single (without 'mpirun -np')"), 3))
                build.MPIrunDeactivated = True
                cmd = []
    else:
        cmd = []

    return cmd


# ==================================================================================================
def copyRestartFile(path, path_target):
    """Copy new restart file into example folder"""
    # Check whether the file for copying exists
    if not os.path.exists(path):
        s = tools.red(f"copyRestartFile: Could not find file=[{path}] for copying")
        print(s)
        sys.exit(1)

    # Check whether the destination for copying the file exists
    if not os.path.exists(os.path.dirname(path_target)):
        s = tools.red(f"copyRestartFile: Could not find location=[{os.path.dirname(path_target)}] for copying")
        print(s)
        sys.exit(1)

    # Copy file and create new reference
    shutil.copy(path, path_target)
    s = tools.yellow(f"New restart file is copied from file=[{path}] to file=[{path_target}]")
    print(s)


# ==================================================================================================
class Externals(OutputDirectory):
    def __init__(self, parameters, example, number):  # noqa: ARG002
        self.parameters = parameters
        OutputDirectory.__init__(self, example, '', -1)

    def __str__(self):
        s = "external parameters:\n"
        s += ",".join([f"{k}: {v}" for k, v in self.parameters.items()])
        return tools.indent(s, 2)


def getExternals(path, example, build):
    # fmt: off
    externals_pre    = []
    externals_post   = []
    externals_errors = []
    # fmt: on

    # Get combinations from externals.ini
    if not os.path.exists(path):
        return externals_pre, externals_post, externals_errors
    combis, _ = combinations.getCombinations(path)

    for iCombi, combi in enumerate(combis):
        # Check directory
        externaldirectory = combi.get('externaldirectory', None)
        if (not externaldirectory or not os.path.exists(os.path.join(example.source_directory, externaldirectory))) and not externaldirectory.endswith('.ini'):  # string is or empty and path does not exist
            s = tools.red(f'getExternals: "externaldirectory" is empty or the path [{os.path.join(example.source_directory, externaldirectory)}] does not exist')
            externals_errors.append(s)
            print(s)
            ExternalRun.total_errors += 1  # add error if externalrun fails
            continue

        # Check binary
        binary_found = False  # default
        s = ''  # default
        externalbinary = combi.get('externalbinary', None)
        if not externalbinary:
            s = tools.red(f'getExternals: External tools binary path "externalbinary" has not been supplied for external run number {iCombi} with "externaldirectory"=[{externaldirectory}].')
            externals_errors.append(s)
            print(s)
            ExternalRun.total_errors += 1  # add error if externalrun fails
            continue
        # Get binary name
        binary = os.path.basename(externalbinary).lower()

        # First: Check for the binary under ./bin directory (ignore the original full path)
        # > Pre-compiled mode: Use only the binary name
        # > Build mode: Use the complete binary path
        binary_path = os.path.abspath(os.path.join(build.binary_dir, binary)) if isinstance(build, Standalone) else os.path.abspath(os.path.join(build.binary_dir, externalbinary))

        # Second: If the binary path does not exist
        # 1.) Check the original binary path, e.g., ./hopr/build/bin/hopr
        # 2.) for specific binaries, e.g., hopr: check the environment variable: HOPR_PATH
        if os.path.exists(binary_path):
            binary_found = True
        else:
            # If the binary is not within the ./bin/ directory, check if the path points to a binary directly
            binary_path = os.path.abspath(externalbinary)
            if os.path.exists(binary_path):
                binary_found = True
            elif binary == 'hopr':
                # Try and load hopr binary path form environment variables
                hopr_path = os.getenv('HOPR_PATH')
                if hopr_path and os.path.exists(hopr_path):
                    binary_path = hopr_path
                    binary_found = True
                    combi['externalbinary'] = binary  # over-write user-defined path
                else:  # fmt: skip
                    s = f'Tried loading hopr binary path from environment variable $HOPR_PATH=[{hopr_path}] as the supplied path does not exist.\nAdd the binary path via "export HOPR_PATH=/opt/hopr/1.X/bin/hopr"\n'
            elif binary == 'pyhope':
                # Try and load hopr binary path form environment variables
                pyhope_path = shutil.which("pyhope")
                if pyhope_path:
                    binary_path = pyhope_path
                    binary_found = True
                    combi['externalbinary'] = pyhope_path  # over-write user-defined path
                else:  # fmt: skip
                    s = f'Tried loading pyhope binary path from environment (pyhope_path = {pyhope_path}), but it was not found."\n'

            # Display error if no binary is found
            if not binary_found:
                s = tools.red(f'getExternals: {s}The supplied path [{binary_path}] via "externalbinary" does not exist.')
                externals_errors.append(s)
                print(s)
                ExternalRun.total_errors += 1  # add error if externalrun fails
                continue

        # If the binary has been found, assign pre/post flag
        if binary_found:
            combi['binary_path'] = binary_path
            if combi.get('externalruntime', '') == 'pre':
                externals_pre.append(Externals(combi, example, -1))
            elif combi.get('externalruntime', '') == 'post':
                externals_post.append(Externals(combi, example, -1))
            else:
                s = tools.red('External tools is neither "pre" nor "post".')
                externals_errors.append(s)
                print(s)
                ExternalRun.total_errors += 1  # add error if externalrun fails
                continue

    return externals_pre, externals_post, externals_errors


# ==================================================================================================
class ExternalRun(OutputDirectory, ExternalCommand):
    total_errors = 0
    total_number_of_runs = 0

    def __init__(self, parameters, parameterfilepath, external, number, digits, externalruns=True):  # noqa: ARG002
        # fmt: off
        self.successful         = True
        self.globalnumber       = -1
        self.analyze_results    = []
        self.analyze_successful = True
        self.parameters         = parameters
        self.digits             = digits
        self.source_directory   = os.path.dirname(parameterfilepath)
        # fmt: on

        OutputDirectory.__init__(self, external, '', -1, mkdir=False)
        ExternalCommand.__init__(self)

        # external folders already there
        self.skip = False

    def execute(self, build, external, args, meshes_directory=None, mesh_generator=None):
        ''' '
        Arguments:  - build
                    - external
                    - args
                    - meshes_directory:     directory where meshes are stored
                    - mesh_generator:       name of the external which creates meshes (defaults in PerformCheck.__init__())
        '''
        # set path to parameter file (single combination of values for execution "parameter.ini" for example)
        self.parameter_path = os.path.join(external.directory, external.parameterfile)

        # create parameter file with one set of combinations
        combinations.writeCombinationsToFile(self.parameters, self.parameter_path)

        # check MPI threads for mpirun
        MPIthreads = external.parameters.get('MPI')

        # check MPI built binary (only possible for reggie-compiled binaries)
        cmd = SetMPIrun(build, args, MPIthreads)

        # Get binary path
        binary_path = external.parameters.get('binary_path')

        cmd.append(binary_path)
        cmd.append(external.parameterfile)
        # append suffix commands, e.g., a second parameter file 'DSMC.ini' or '-N 12'
        cmd_suffix = external.parameters.get('cmd_suffix')
        if cmd_suffix:
            cmd.append(cmd_suffix)

        # Command for executing beforehand
        cmd_pre_execute = external.parameters.get('cmd_pre_execute')
        if cmd_pre_execute:
            cmd_pre = cmd_pre_execute.split()
            s = "Running [{}] ...".format(" ".join(cmd_pre))
            # if meshes are reused the 'cp' command needs to be adjusted to handle the symbolic links correctly
            if args.meshesdir and 'cp' in cmd_pre:
                copy_index = cmd_pre.index('cp')
                if re.match(r'^-[a-zA-Z]+$', cmd_pre[copy_index + 1]) and 'L' not in cmd_pre[copy_index + 1]:
                    # add -L to the options of the copy command
                    cmd_pre[copy_index + 1] = cmd_pre[copy_index + 1] + 'L'
                else:
                    # if not options are given, add -L to the copy command
                    cmd_pre.insert(copy_index + 1, '-L')
            self.execute_cmd(cmd_pre, external.directory, name='pre-exec', string_info=tools.indent(s, 3))  # run something

        if self.return_code != 0:
            self.successful = False
            return None

        # check if the command 'cmd' can be executed
        cmdstr = " ".join(cmd)
        if self.return_code != 0:
            print(tools.indent("Cannot run the code: " + s, 2))
        else:
            s = f"Running [{cmdstr}] ..."
            _, tail = os.path.split(binary_path)
            # create meshes in separate directory to reuse same meshes with symbolic links, only if meshes_directory is not None and mesh_generator is external
            if meshes_directory is not None and mesh_generator in tail:
                # copy hopr.ini file to meshes_directory
                shutil.copy2(self.parameter_path, meshes_directory)
                # create list of all files in current directory
                current_dir_files = [f for f in os.listdir(os.path.dirname(self.parameter_path)) if os.path.isfile(os.path.join(os.path.dirname(self.parameter_path), f))]
                # check if any other files in current directory are needed for hopr execution
                matching_files = [f for f in current_dir_files if f in list(self.parameters.values())]
                if matching_files:
                    for file in matching_files:
                        full_path = os.path.join(os.path.dirname(self.parameter_path), file)  # Create full path
                        if os.path.isfile(full_path):
                            # copy matching files to meshes_directory
                            shutil.copy2(os.path.join(os.path.dirname(self.parameter_path), file), meshes_directory)
                        else:
                            print(tools.red(f"File [{file}] does not exist in the current directory."))
                            self.successful = False
                            return None
                # execute hopr in meshes_directory
                self.execute_cmd(cmd, meshes_directory, name=tail, string_info=tools.indent(s, 3))  # run the code
            else:
                self.execute_cmd(cmd, external.directory, name=tail, string_info=tools.indent(s, 3))  # run the code

        if self.return_code != 0:
            self.successful = False

        return cmdstr

    def __str__(self):
        s = "RUN parameters:\n"
        s += ",".join([f"{k}: {v}" for k, v in self.parameters.items()])
        return tools.indent(s, 3)


def getExternalRuns(parameterfilepath, external):
    """Get all combinations in 'parameter.ini'"""
    externalruns = []
    # get combis : for each externalrun a combination of parameters is stored in a dict containing a [key]-[value] pairs
    #              combis contains multiple dicts 'OrderedDict'
    #              example for a key = 'N' and its value = '5' for polynomial degree of 5
    #     digits : contains the number of variations for each [key]
    #              example in parameter.ini: N = 1,2,3 then digits would contain OrderedDict([('N', 2),...) for 0,1,2 = 3 different
    #              values for N)
    combis, digits = combinations.getCombinations(parameterfilepath, CheckForMultipleKeys=True)  #  parameterfilepath = path to parameter.ini (source)
    for i, parameters in enumerate(combis):
        # check each [key] for empty [value] (e.g. wrong definition in parameter.ini file)
        for key, value in parameters.items():
            if not value:
                raise Exception(tools.red(f'parameter.ini contains an empty parameter definition for [{key}]. Remove unnecessary commas!'))

        # construct run information with one set of parameters (parameter.ini will be created in target directory when the setup
        # is executed), one set of command line options (e.g. mpirun information) and the info of how many times a parameter is
        # varied under the variable 'digits'
        run = ExternalRun(parameters, parameterfilepath, external, i + 1, digits)

        # check if the run cannot be performed due to problems encountered when setting up the folder (e.g. not all files could
        # be create or copied to the target directory)
        if not run.skip:
            externalruns.append(run)  # add/append the run to the list of externalruns
    return externalruns


# ==================================================================================================
class Run(OutputDirectory, ExternalCommand):
    total_errors = 0
    total_number_of_runs = 0

    def __init__(self, parameters, path, command_line, number, digits):
        # fmt: off
        self.successful         = True
        self.globalnumber       = -1
        self.analyze_results    = []
        self.analyze_successful = True
        self.parameters         = parameters
        self.digits             = digits
        self.source_directory   = os.path.dirname(path)
        # fmt: on

        OutputDirectory.__init__(self, command_line, 'run', number, mkdir=False)
        ExternalCommand.__init__(self)

        self.skip = os.path.exists(self.target_directory)
        if self.skip:
            return

        tools.create_folder(self.target_directory)

        # copy all files in the source directory (example) to the target directory: always overwrite
        for f in os.listdir(self.source_directory):
            src = os.path.abspath(os.path.join(self.source_directory, f))
            dst = os.path.abspath(os.path.join(self.target_directory, f))
            if os.path.isdir(src):  # check if file or directory needs to be copied
                if os.path.basename(src) != 'output_dir':  # do not copy the output_dir recursively into itself! (infinite loop)
                    shutil.copytree(src, dst)  # copy tree
            else:
                # Check for symbolic links
                if os.path.islink(src):
                    # Do not copy broken symbolic links
                    if os.path.exists(src):
                        shutil.copyfile(src, dst)  # copy symbolic link
                else:
                    shutil.copyfile(src, dst)  # copy file

    def rename_failed(self):
        """
        Rename failed run directories in order to repeat the run when the regression check is repeated.

        This routine is called if either the execution fails or an analysis.
        """
        shutil.rmtree(self.target_directory + "_failed", ignore_errors=True)  # remove if exists
        shutil.move(self.target_directory, self.target_directory + "_failed")  # rename folder (non-existent folder fails)
        self.target_directory = self.target_directory + "_failed"  # set new name for summary of errors

    def execute(self, build, command_line, args, external_failed):
        Run.total_number_of_runs += 1
        self.globalnumber = Run.total_number_of_runs

        # Check if a possible pre-processing step has failed.
        # If so, do not run the code as the assumption is that it depends on a positive output of the (pre) external
        if external_failed:
            self.successful = False
            self.rename_failed()
            s = tools.red(tools.indent("Cannot run the code because the (pre) external run failed.", 2))
            print(s)
            return

        # set path to parameter file (single combination of values for execution "parameter.ini" for example)
        self.parameter_path = os.path.join(self.target_directory, "parameter.ini")

        # create parameter file with one set of combinations
        combinations.writeCombinationsToFile(self.parameters, self.parameter_path)

        # Get MPI threads for mpirun
        MPIthreads = command_line.parameters.get('MPI')

        # Safety check: Get the mesh file, then extract the number of elements using h5py, limit the number of mpithreads
        if h5py_module_loaded:
            try:
                # Get the mesh file
                MeshFileName = combinations.readValueFromFile(self.parameter_path, 'MeshFile')

                # Extract the number of elements using h5py
                with h5py.File(os.path.join(self.target_directory, MeshFileName), 'r') as MeshFile:
                    nElems = MeshFile.attrs['nElems']

                    # hopr stores the number of elements as array with one element
                    try:
                        nElems = int(nElems[0])
                    # pyhope stores number of elements as scalar
                    except (TypeError, IndexError):
                        nElems = int(nElems)

                    # Limit the number of mpithreads
                    if MPIthreads:
                        if int(MPIthreads) > nElems:
                            s = tools.yellow(f"Automatically reducing number of MPI threads from {int(MPIthreads)} to {nElems} (number of elements in mesh)!")
                            print(tools.indent(s, 2))
                        MPIthreads = str(min(nElems, int(MPIthreads)))
            except Exception as e:
                s = (
                    "Failed to extract the number of elements ('nElems') from the mesh file to automatically limit MPI threads."
                    f"\nError message: [{e}]\nThe program will continue without limiting MPI threads for this execution."
                )
                print(tools.indent(tools.red(s), 2))

        # check MPI built binary (only possible for reggie-compiled binaries)
        cmd = SetMPIrun(build, args, MPIthreads)

        cmd.append(build.binary_path)
        if 'python' not in build.binary_path:
            cmd.append("parameter.ini")

        # append suffix commands, e.g., a second parameter file 'DSMC.ini' or '-N 12'
        cmd_suffix = command_line.parameters.get('cmd_suffix')
        if cmd_suffix:
            cmd.append(cmd_suffix)

        # append restart file name
        cmd_restart_file = command_line.parameters.get('restart_file')
        if cmd_restart_file:
            # check if file exists
            cmd_restart_file_abspath = os.path.abspath(os.path.join(self.target_directory, cmd_restart_file))
            found = os.path.exists(cmd_restart_file_abspath)

            # Check if restartcopy is activated (if true start the simulation at t=0 and copy (create/replace if already exists) to example directory
            if args.restartcopy:
                s = tools.yellow("Restart file copy activated. Starting fresh simulation at t=0.")
                print(tools.indent(s, 2))
            else:  # default
                if not found:
                    self.return_code = -1
                    self.result = tools.red("Restart file not found")
                    s = tools.red(f"Restart file [{cmd_restart_file}] not found under [{cmd_restart_file_abspath}]")
                else:
                    cmd.append(cmd_restart_file)

        # check if the command 'cmd' can be executed
        if self.return_code != 0:
            s = tools.red(tools.indent("Cannot run the code: " + s, 2))
            print(s)
        else:
            s = "Running [{}] ...".format(" ".join(cmd))
            self.execute_cmd(cmd, self.target_directory, string_info=tools.indent(s, 2))  # run the code

        # Copy restart file if required
        if cmd_restart_file and args.restartcopy:
            # 1. Get directory path and filename of the originally required restart file
            _, tail = os.path.split(cmd_restart_file_abspath)

            # 2. File path to be copied
            restart_file_path = os.path.abspath(os.path.join(self.target_directory, tail))

            # Check whether the newly created restart file actually has a different name than the one supplied in cmd_restart_file,
            # e.g., [Test_State_000.000000.h5] instead of [Test_State_000.000000_restart.h5] the first will be the source and the
            # latter will be the target file name
            try:
                file_name = os.path.join(self.target_directory, 'std.out')
                with open(file_name) as f:
                    for line in f:  # iterate over all lines of the file
                        if 'WRITE STATE TO HDF5 FILE' in line:
                            s = line.rstrip()
                            FileName = re.search(r'\[(.*?)\]', s).group(1)  # search for string within parenthesis [...] and check if that is a file that exists
                            replace_restart_file_path = os.path.join(self.target_directory, FileName)
                            print(f"replace_restart_file_path = {replace_restart_file_path}")
                            print(f"os.path.isfile(replace_restart_file_path) = {os.path.isfile(replace_restart_file_path)}")
                            if replace_restart_file_path and os.path.isfile(replace_restart_file_path):
                                print(tools.yellow(f"Found replacement for restart file copy: [{FileName}] instead of [{cmd_restart_file}]"))
                                restart_file_path = replace_restart_file_path
                                cmd_restart_file = FileName
                            break
            except Exception as e:
                print(tools.red(f"Tried getting the first State file name from std.out. Failed. Using original restart file for copying: [{cmd_restart_file}]"))
                print(tools.red(f"e = {e}"))

            # 3. Target file path
            restart_file_path_target = os.path.join(self.source_directory, tail)

            # 4. Check if the file for copying exists
            found = os.path.exists(restart_file_path)
            if not found:
                # Restart file not found (or not created)
                self.return_code = -1
                self.result = tools.red(f"Restart file [{cmd_restart_file}] was not created")
                s = tools.red(f"Restart file (which should have been created) [{cmd_restart_file}] not found under [{restart_file_path}]")
                print(s)
            else:
                # Copy new restart file
                copyRestartFile(restart_file_path, restart_file_path_target)
                s = tools.yellow("Run(OutputDirectory, ExternalCommand): performed restart file copy!")
                print(s)

        if self.return_code != 0:
            self.successful = False
            self.rename_failed()

    def __str__(self):
        s = "RUN parameters:\n"
        s += ",".join([f"{k}: {v}" for k, v in self.parameters.items()])
        return tools.indent(s, 3)


def getRuns(path, command_line):
    """Get all combinations in 'parameter.ini'"""
    runs = []
    # get combis : for each run a combination of parameters is stored in a dict containing a [key]-[value] pairs
    #              combis contains multiple dicts 'OrderedDict'
    #              example for a key = 'N' and its value = '5' for polynomial degree of 5
    #     digits : contains the number of variations for each [key]
    #              example in parameter.ini: N = 1,2,3 then digits would contain OrderedDict([('N', 2),...) for 0,1,2 = 3 different
    #              values for N)
    combis, digits = combinations.getCombinations(path, CheckForMultipleKeys=True)  # path to parameter.ini (source)
    for i, parameters in enumerate(combis):
        # check each [key] for empty [value] (e.g. wrong definition in parameter.ini file)
        for key, value in list(parameters.items()):
            if not value:
                raise Exception(tools.red(f'parameter.ini contains an empty parameter definition for [{key}]. Remove unnecessary commas!'))
        # construct run information with one set of parameters (parameter.ini will be created in target directory when the setup
        # is executed), one set of command line options (e.g. mpirun information) and the info of how many times a parameter is
        # varied under the variable 'digits'
        run = Run(parameters, path, command_line, i + 1, digits)
        # check if the run cannot be performed due to problems encountered when setting up the folder (e.g. not all files could
        # be create or copied to the target directory)
        if not run.skip:
            runs.append(run)  # add/append the run to the list of runs
    return runs


class ThreadSafeStdout:
    # save  current stdout and create new thread local attribute
    def __init__(self):
        self.original_stdout = sys.stdout
        self.local = threading.local()

    # catch print statements (sys.stdout.write(..) with local stream, if present and default back to original stdout)
    def write(self, data):
        getattr(self.local, 'stream', self.original_stdout).write(data)

    def flush(self):
        getattr(self.local, 'stream', self.original_stdout).flush()

    # copy other build-in methods and attributes of original stdout
    def __getattr__(self, name):
        return getattr(getattr(self.local, 'stream', self.original_stdout), name)


class PerformCheck:
    def __init__(self):
        # Definition looks like: self.MeshGeneration = {'external': 'end of created filename', ...}
        self.MeshGeneration = {'pyhope': '_mesh.h5', 'hopr': '_mesh.h5'}

    ###################################################################################
    ############################ Single external functions ############################
    ###################################################################################
    def mesh_external(self, run, external, externalrun, build, args):
        '''
        This function executes all externals for the first run of the first command line and creates a dictionary with symbolic links to the created files
        This is (currently) only used for the mesh generation with pyhope (or originally hopr) to create all meshes only once and reuse them in later runs
        Extension for other mesh generators or generally externals (following the same pattern to use symbolic links) can be done with the self.MeshGeneration dictionary
        Note that the name in the self.MeshGeneration dictionary has to appear in the name of the externalbinary
        '''

        if not os.path.exists(self.meshes_dir_path):
            os.makedirs(self.meshes_dir_path)
            print(tools.indent(tools.yellow(f'Meshes will be stored in directory: {self.meshes_dir_path}'), 3))

        # dummy return for externalrun which should be skipped
        externalcmd = ''

        mesh_name_current_run = os.path.basename(run.parameters['MeshFile'])
        # execute external if mesh not found in directoy of meshes
        if not self.mesh_externals_done:
            externalcmd = externalrun.execute(build, external, args, meshes_directory=self.meshes_dir_path, mesh_generator=self.Matched_Generator_Name)
            # collect all mesh names which have been created in the directory 'self.meshes_dir_path' (since name of the mesh is not part of externalrun.parameters)
            for file in os.listdir(self.meshes_dir_path):
                # create identifier of external, externalparameterfile and externalrun to check if mesh for given combination of these there has been build already
                # external_count and externalparameterfiel_count prevent from using the same mesh even hopr/pyhope is listed twice as external with separate .ini files
                dict_identifier = f'{self.external_count}' + f'{self.externalparameterfile_count}'
                # we use the self.Matched_Generator_Name to get the corresponding file ending of our MeshGenerator from MeshGeneration
                if file.endswith(self.MeshGeneration[self.Matched_Generator_Name]):
                    full_path = os.path.join(self.meshes_dir_path, file)
                    if os.path.isfile(full_path) and full_path not in self.created_mesh_files.values():
                        dict_identifier = dict_identifier + f'{self.externalrun_count}'
                        # save directory where mesh is stored for current combination to set symbolic link in next run/command_line run
                        self.created_mesh_files[dict_identifier] = os.path.join(self.meshes_dir_path, file)

        # neither the first command_line or run so the mesh should have been created already, so we create the identifier to check our dict
        dict_identifier = f'{self.external_count}' + f'{self.externalparameterfile_count}' + f'{self.externalrun_count}'

        # self.created_mesh_files contains dict_identifier as keys and the link to the corresponding mesh
        try:
            mesh_name_current_externalrun = os.path.basename(self.created_mesh_files[dict_identifier])
        except Exception as e:
            print(tools.indent(tools.red(f"Mesh not found in {self.meshes_dir_path}\nException type: {type(e).__name__}\nException value: {e!r}\n"), 2))
            sys.exit(1)

        # check if mesh of current run matches mesh of current external run to set symbolic link
        if mesh_name_current_run == mesh_name_current_externalrun:
            relative_source_path = os.path.relpath(self.created_mesh_files[dict_identifier], external.directory)
            target_mesh_path = os.path.join(external.directory, mesh_name_current_run)
            # Since external will not be executed for these runs check if pre-execution is needed (for the first run the pre_execution is done inside the externalrun execution)
            if self.command_line_count != 1 or self.RunCount != 1:
                cmd_pre_execute = external.parameters.get('cmd_pre_execute')
                if cmd_pre_execute:
                    cmd_pre = cmd_pre_execute.split()
                    s = "Running [{}] ...".format(" ".join(cmd_pre))
                    externalrun.execute_cmd(cmd_pre, external.directory, name='pre-exec', string_info=tools.indent(s, 3))  # run something
            try:
                # Create symbolic link
                os.symlink(relative_source_path, target_mesh_path)
                print(tools.indent(tools.yellow(f'Creating symbolic link from {relative_source_path} to {target_mesh_path}'), 3))
            except Exception as e:
                print(tools.indent(tools.red(f'Error creating symbolic link:{e}'), 3))
            # dummpy output for error message if external fails
            externalcmd = f'ln -s {relative_source_path} {target_mesh_path}'

        # return externalcmd for error message if externalrun fails
        return externalcmd

    ############################################################################
    ############################ Coverage functions ############################
    ############################################################################
    def init_coverage(self, args):
        '''
        This function initializes the flags and directory needed for coverage information
        '''

        # get coverage flags and set output format
        self.coverage_env = os.getenv('CODE_COVERAGE')
        self.coverage_output_html = False
        self.coverage_output_cobertura = False
        if self.coverage_env:
            args.coverage = True
        elif args.coverage:  # check for command line argument when executed locally
            if args.coverage == '0':
                pass
            elif all(c in '12' for c in args.coverage):
                if '1' in args.coverage:
                    self.coverage_output_html = True
                if '2' in args.coverage:
                    self.coverage_output_cobertura = True
            else:
                print(tools.red(f"Invalid value for --coverage: '{args.coverage}'. Use any combination of 1, 2 or 0."))
                sys.exit(1)
            args.coverage = True

        # create directory to store coverage data (one file per build), if executed locally the coverage directory is created in the current directory, but
        # for GitLab regressiontests the parent dir is used since all build directories will be deleted but the coverage data is needed
        if args.coverage:
            if self.coverage_env:
                self.coverage_dir = os.path.abspath(os.path.join(os.path.dirname(os.getcwd()), 'Coverage'))
            else:
                self.coverage_dir = os.path.abspath(os.path.join(OutputDirectory.output_dir, 'Coverage'))
            tools.create_folder(self.coverage_dir)

    def write_single_coverage_report(self, build, args):
        '''
        This function writes a coverage report for the current build with gcovr:

        gcovr needs two directories as arguments: - "source_files_dir" the root directory, where the source files are located
                                                  - "coverage_files_dir" the coverage files directory (containing necessary files)
        When compiling with the --coverage option, the compiler generates additional files for each object file, .gcno and .gcda
        The .gcno file is created during compilation and contains information for reconstructing basic block graphs and associating source lines with blocks.
        The .gcda file is generated when the instrumented code is executed and contains counts
        for out of source builds (like with cmake) these are not the same directory
        the .gcno and .gcda files are located in the build/CMakeFiles directory, but the build directory is sufficient here
        for a standalone executable these paths are not safely known here and are therefore searched
        for the args.basedir options is gets easier
        '''

        s = tools.green("Post-processing: Started gcovr")
        print(tools.indent(s, 1))
        if args.exe:
            print(tools.indent(tools.yellow(f"Running gcovr for standalone executable [{build.binary_path}]"), 2))
            try:
                # expect directory structure for a cmake project like
                # program
                # |- src
                #   | - source files
                #   | ...
                # |- build
                #   | - bin
                # default to the parent dir of binary_dir
                coverage_files_dir = os.path.dirname(build.binary_dir)
                # sanity check: find .gcno files in coverage_files_dir or any subdir, since exe must be compiled with coverage
                gcno_files = [os.path.join(root, file) for root, _, files in os.walk(coverage_files_dir) for file in files if file.endswith('.gcno')]
                if not gcno_files:
                    raise Exception(f"No .gcno files found in coverage_files_dir [{coverage_files_dir}] or any subdirectories. Please check if the executable is compiled with coverage enabled")
            except Exception as e:
                print("{}".format(tools.red(f"Error determining source directory of standalone executable: {e}")))
                sys.exit(1)
            # source_files_dir is the directory where the source files are located, it is expected to be the parent directory or at least a subdirectory of the parentdirectory
            self.source_files_dir = os.path.dirname(coverage_files_dir)
        else:
            coverage_files_dir = build.binary_dir
            self.source_files_dir = build.basedir

        coverage_files_dir = os.path.abspath(coverage_files_dir)
        if not os.path.exists(coverage_files_dir):
            s = tools.red(f"Coverage data object directory [{coverage_files_dir}] does not exist")
            print(s)
            sys.exit(1)

        # try to append /src to the path to exclude other directories, e.g. UnitTests
        src_path = os.path.abspath(os.path.join(self.source_files_dir, 'src'))
        if os.path.exists(src_path):
            self.source_files_dir = src_path
            print(tools.indent(tools.yellow(f"Using source directory [{self.source_files_dir}] for gcovr"), 2))

        self.source_files_dir = os.path.abspath(self.source_files_dir)
        if not os.path.exists(self.source_files_dir):
            s = tools.red(f"Source files directory [{self.source_files_dir}] does not exist")
            print(s)
            sys.exit(1)

        s = tools.indent(tools.green(f'Combining coverage reports for build: {build.target_directory}'), 2)
        print(s)
        cmd_gcovr = ["gcovr", "--root", f"{self.source_files_dir}", f"{coverage_files_dir}"]
        if args.debug > 0:
            cmd_gcovr.extend(["--verbose", "--print-summary"])

        if args.gcovr_extra:
            cmd_gcovr.extend(args.gcovr_extra.split(' '))

        # get name of current build source dir
        # > get cwd for naming convention due to gitlab setup
        # > get build_dir name otherwise
        report_name = f"combined_report_{os.path.basename(os.getcwd())}.json" if self.coverage_env else f"combined_report_{os.path.basename(str(coverage_files_dir))}.json"

        # check if file already exists from other reggie call before the current call, e.g. two regression tests use the same build, which would lead to the same report_name here
        if report_name in os.listdir(self.coverage_dir):
            # Locally this won't be a problem, since the coverage files still contain all data from previous runs, but on GitLab/GitHub this might be different
            # The updated .gcno files (which contain the coverage from the current run) in the build directory are not necessarily pushed to the cache (on gitlab) each time the reggie is executed
            # Therefore the coverage information from previous runs is lost and if gcovr is executed again with the same report_name only the coverage data of the last executed run per build is saved
            # This leaves us with two options: Either combine the reports if the name already exists, or just save as a new file
            # Saving new files for each test using the same build could lead to a large amount of report files being cached, so we will combine the reports here

            # Generate new coverage report with temporary name
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', dir=self.coverage_dir, delete=False) as tmp_new:
                temp_new_path = tmp_new.name

            print(tools.indent(f'{report_name} already exists in {self.coverage_dir}! Creating temporary report {os.path.basename(temp_new_path)} and merging coverage data.', 2))
            cmd_gcovr.extend(["--json", os.path.basename(temp_new_path)])
            s = tools.indent("Generating new coverage data [{}] ...".format(" ".join(cmd_gcovr)), 2)
            return_code = ExternalCommand().execute_cmd(cmd_gcovr, self.coverage_dir, string_info=s)
            if return_code != 0:
                # Clean up temp file on failure
                if os.path.exists(temp_new_path):
                    os.remove(temp_new_path)
                raise Exception("Failed to generate new coverage report")

            # Combine old and new reports into temporary output, then move temporary output to report_name
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', dir=self.coverage_dir, delete=False) as tmp_combined:
                temp_combined_path = tmp_combined.name

            # fmt: off
            cmd_combine = ["gcovr",
                        "--root",self.source_files_dir,
                        "--json-add-tracefile",report_name,
                        "--json-add-tracefile",os.path.basename(temp_new_path),
                        "--merge-mode-functions=merge-use-line-min",
                        "--json",os.path.basename(temp_combined_path),
                        ]
            # fmt: on

            s = tools.indent("Merging coverage reports [{}] ...".format(" ".join(cmd_combine)), 2)
            return_code = ExternalCommand().execute_cmd(cmd_combine, self.coverage_dir, string_info=s)
            if return_code != 0:
                # Clean up temp files on failure
                os.remove(temp_new_path)
                os.remove(temp_combined_path)
                raise Exception("Failed to merge coverage reports")

            # rename combined report to final name
            final_path = os.path.join(self.coverage_dir, report_name)
            os.replace(temp_combined_path, final_path)
            # Clean up temporary new report
            os.remove(temp_new_path)
        else:
            # No existing report, create new one directly
            cmd_gcovr.extend(["--json", report_name])
            s = tools.indent("Running [{}] ...".format(" ".join(cmd_gcovr)), 2)
            ExternalCommand().execute_cmd(cmd_gcovr, self.coverage_dir, string_info=s)

        s = tools.green("Post-processing: Finished gcovr")
        print(tools.indent(s, 1))

    def combine_local_coverage_reports(self, args):
        '''
        This function uses all coverage reports which were created by the current reggie call and combines them into one report
        Only used for local execution
        '''

        combined_cov_path = os.path.abspath(os.path.join(self.coverage_dir, "combined_report"))
        tools.create_folder(combined_cov_path)

        coverage_files = [os.path.abspath(os.path.join(self.coverage_dir, file)) for file in os.listdir(self.coverage_dir) if file.endswith('.json')]

        # combine all coverage reports from all builds
        s = tools.indent(tools.green('Combining coverage reports for all builds'), 1)
        print(s)
        cmd_combine = ["gcovr", "--root", f"{self.source_files_dir}"]
        if args.debug > 0:
            cmd_combine.extend(["--verbose", "--print-summary"])
        # add files separately to the command line since ExternalCommand().execute_cmd resolves wildcards which would lead to invalid syntax for gcovr
        # which is either --json-add-tracefile file1 --json-add-tracefile file2 or --json-add-tracefile *.json, but ExternalCommand().execute_cmd resolves wildcards to
        # --json-add-tracefile file1 file2 ...
        for cov_file in coverage_files:
            cmd_combine.extend(["--json-add-tracefile", f"{cov_file}"])
        # use merge mode functions to avoid errors if the same functions appears in different lines (e.g. for two builds a block is missing due to compiler flags, which moves func1 form line X to X-5)
        cmd_combine.append("--merge-mode-functions=merge-use-line-min")
        if self.coverage_output_html:
            html_path = os.path.abspath(os.path.join(combined_cov_path, "html"))
            tools.create_folder(html_path)
            cmd_combine_html = cmd_combine.copy()
            cmd_combine_html.extend(["--html-nested", "combined_report.html"])
            s = tools.indent("Running [{}] ...".format(" ".join(cmd_combine_html)), 2)
            ExternalCommand().execute_cmd(cmd_combine_html, html_path, string_info=s)
        if self.coverage_output_cobertura:
            xml_path = os.path.abspath(os.path.join(combined_cov_path, "xml"))
            tools.create_folder(xml_path)
            cmd_combine_cobertura = cmd_combine.copy()
            cmd_combine_cobertura.extend(["--cobertura", "combined_report.xml"])
            s = tools.indent("Running [{}] ...".format(" ".join(cmd_combine_cobertura)), 2)
            ExternalCommand().execute_cmd(cmd_combine_cobertura, xml_path, string_info=s)

        cmd_combine.extend(["--json", "combined_report.json"])
        # merge functions for builds with different compiler flags (function name stays the same but line changes due to ifdef)
        s = tools.indent("Running [{}] ...".format(" ".join(cmd_combine)), 2)
        ExternalCommand().execute_cmd(cmd_combine, combined_cov_path, string_info=s)

    #######################################################################
    ############################ main function ############################
    #######################################################################
    def main(self, start, builds, args, log):
        """
        General workflow:

        1.   loop over alls builds
        1.1    read all example directories in the check directory and exit if no examples are found
        1.2    compile the build if args.run is false and the binary is non-existent
        1.3    check whether the build is using MPI
        2.   loop over all example directories
        2.1    read the command line options in 'command_line.ini' for binary execution (e.g. number of threads for mpirun)
        2.2    read the restart file list
        2.3    read the analyze options in 'analyze.ini' within each example directory (e.g. L2 error analyze)
        3.   loop over all command_line options
        3.1    read the executable parameter file 'parameter.ini' (e.g. flexi.ini with which flexi will be started)
        4.   loop over all parameter combinations supplied in the parameter file 'parameter.ini'
        4.1    read the external options in 'externals.ini' within each example directory (e.g. eos, hopr, posti)
        (pre)  perform a preprocessing step: e.g. run hopr, eos, ...
            (1):   loop over all externals available in external.ini
            (1.1):   get the path and the parameterfiles to the i'th external
            (2):   loop over all parameterfiles available for the i'th external
            (2.1):   consider combinations
            (3):   loop over all combinations and parameter files for the i'th external
            (3.1):   run the external binary
        4.2    execute the binary file for one combination of parameters
        (post) perform a post processing step: e.g. run posti, ...
            (1):   loop over all externals available in external.ini
            (1.1):   get the path and the parameterfiles to the i'th external
            (2):   loop over all parameterfiles available for the i'th external
            (2.1):   consider combinations
            (3):   loop over all combinations and parameter files for the i'th external
            (3.1):   run the external binary
        4.3    remove unwanted files: run analysis directly after each run (as opposed to the normal analysis which is used for analyzing the created output)
        5.   loop over all successfully executed binary results and perform analyze tests
        6.   rename all run directories for which the analyze step has failed for at least one test
        7.   perform analyze tests comparing corresponding runs from different commands
        """

        # compile and run loop
        try:  # if compiling fails -> go to exception
            # initialize coverage
            self.init_coverage(args)

            # 1.   loop over alls builds
            for build_number, build in enumerate(builds, start=1):
                remove_build_when_successful = True
                print("Build Cmake Configuration ", build_number, " of ", len(builds), " ...", end=' ')  # skip linebreak
                log.info(str(build))

                # 1.1    read the example directories
                # get example folders: run_basic/example1, run_basic/example2 from check folder
                build.examples = getExamples(args.check, build, log)
                log.info("build.examples" + str(build.examples))

                # check if no examples are found
                if len(build.examples) == 0:
                    s1 = tools.red("No matching examples found for this build! Create an example or exclude this build combination")
                    s2 = build.configuration.items()
                    s = s1 + '\n' + str(s2)
                    print(s)
                    sys.exit(1)

                # 1.2    compile the build if args.run is false and the binary is non-existent
                if not args.dry_run:
                    build.compile(args.buildprocs)
                if not args.carryon and not args.dry_run:  # remove examples folder if not carryon, in order to re-run all examples
                    tools.remove_folder(os.path.join(build.target_directory, "examples"))

                # 1.3    check whether the build is using MPI (either disabled for the whole reggie execution or because compiled without MPI)
                if args.noMPI or args.noMPIautomatic:
                    MPIbuilt = False
                else:
                    if args.run:
                        # If code is not compiled (ie. an executable is provided, activating MPI)
                        MPIbuilt = True
                    else:
                        # Determining how the executable has been compiled
                        LIBS_USE_MPI = build.configuration.get('LIBS_USE_MPI', 'OFF')
                        if LIBS_USE_MPI == 'ON':
                            MPIbuilt = True
                        else:
                            # Additionally check for variable MPI_built_flag=PICLAS_MPI (or FLEXI_MPI, depending on the executable name)
                            MPI_built_flag = os.path.basename(build.binary_path).upper() + "_MPI"
                            MPI_built_value = build.configuration.get(MPI_built_flag, 'OFF')
                            # > PICLAS_MPI=ON specified
                            # > PICLAS_MPI=OFF or flag not specified (i.e. assuming LIBS_USE_MPI=OFF)
                            MPIbuilt = MPI_built_value == 'ON'
                build.MPIbuilt = MPIbuilt

                # 2.   loop over all example directories
                for example in build.examples:
                    log.info(str(example))
                    print(str(example))

                    # 2.1    read the command line options in 'command_line.ini' for binary execution
                    #        (e.g. number of threads for mpirun)
                    example.command_lines = getCommand_Lines(os.path.join(example.source_directory, 'command_line.ini'), example, MPIbuilt, MaxCores=args.MaxCores)

                    # 2.2   read-in restart_file parameter from command_line.ini separately
                    example.restart_file_list = getRestartFileList(example)

                    # 2.3    read the analyze options in 'analyze.ini' within each example directory (e.g. L2 error analyze)
                    example.analyzes = getAnalyzes(os.path.join(example.source_directory, 'analyze.ini'), example, args)

                    # 3.   loop over all command_line options
                    # create directory containing mesh files to set symbolic links if mesh file is already created
                    if args.meshesdir:
                        self.created_mesh_files = {}
                        self.meshes_dir_path = os.path.join(example.target_directory, 'meshes')
                        # self.mesh_externals_done keeps track if all meshes were created to reuse with symbolic links already
                        self.mesh_externals_done = False
                    for self.command_line_count, self.command_line in enumerate(example.command_lines, start=1):
                        log.info(str(self.command_line))
                        # Database linking
                        self.database_path = self.command_line.parameters.get('database', None)
                        if self.database_path is not None:
                            self.database_path = os.path.abspath(os.path.join(example.source_directory, self.database_path))
                            if not os.path.exists(self.database_path):
                                s = tools.red(f'command_line.ini: cannot find file=[{self.database_path}]')
                                print(s)
                                sys.exit(1)
                        # CVAE scattering linking
                        self.cvae_scattering_cvae = self.command_line.parameters.get('cvae_scattering', None)
                        if self.cvae_scattering_cvae is not None:
                            self.cvae_scattering_cvae = os.path.abspath(os.path.join(example.source_directory, self.cvae_scattering_cvae))
                            if not os.path.exists(self.cvae_scattering_cvae):
                                s = tools.red(f'command_line.ini: cannot find file=[{self.cvae_scattering_cvae}]')
                                print(s)
                                sys.exit(1)

                        # Get the index of the restart file to append to the analyze
                        iRestartFile = example.restart_file_list.index(self.command_line.parameters.get('restart_file', None)) if example.restart_file_list is not None else None

                        # 3.1    read the executable parameter file 'parameter.ini' (e.g. flexi.ini with which
                        #        flexi will be started), N=, mesh=, etc.
                        self.command_line.runs = getRuns(os.path.join(example.source_directory, 'parameter.ini'), self.command_line)

                        # 4.   loop over all parameter combinations supplied in the parameter file 'parameter.ini'
                        if self.command_line.parameters.get('reggie_multicore', 'False').lower() in ['true', 't']:
                            # Calculate the number of worker processes based on available cores and MPI threads
                            cpu_count = getMaxCPUCores()
                            num_processes = max(1, int(cpu_count / int(self.command_line.parameters.get('MPI'))))
                            # parallelization is only implemented per run, so if the number of processes is greater than the number of runs, set it to the number of runs
                            if num_processes > len(self.command_line.runs):
                                num_processes = len(self.command_line.runs)
                                s = tools.yellow(f'Number of workers exceeds number of runs. Using {num_processes} worker(s) instead.')
                            else:
                                s = tools.yellow(f'Using {num_processes} worker(s) for parallel execution.')
                                if num_processes == 1:
                                    s = s.replace('parallel', 'serial')
                            print(tools.indent(s, 1))
                        else:
                            num_processes = 1

                        if num_processes != 1:
                            self.perform_runs_in_parallel(build, example, args, num_processes)
                        else:
                            for RunCount, run in enumerate(self.command_line.runs, start=1):
                                self.perform_single_run(build, example, args, (RunCount, len(self.command_line.runs), run))

                        # 5.   loop over all successfully executed binary results and perform analyze tests
                        runs_successful = [run for run in self.command_line.runs if run.successful]
                        if runs_successful:  # do analysis only if runs_successful is not empty
                            for analyze in example.analyzes:
                                if isinstance(analyze, (Clean_up_files, Analyze_compare_across_commands)):
                                    # skip because either already called in the "run" loop under 4.2 or called later under cross-command comparisons in 7.
                                    continue
                                # Set the restart file index in case of one diff per restart file (from command line)
                                analyze.iRestartFile = iRestartFile
                                # Output of the __str__ for the respective analyze routine
                                print(tools.indent(tools.blue(str(analyze)), 2))
                                # Perform the analyze for the successful runs
                                analyze.perform(runs_successful)
                                # Check if immediate stop is activated on failure
                                if args.stop and Analyze.total_errors > 0:
                                    s = tools.red('Stop on first error (-p, --stop) is activated! Analysis failed')
                                    print(s)
                                    sys.exit(1)
                        else:  # don't delete build folder after all examples/runs
                            remove_build_when_successful = False

                        # 6.   rename all run directories for which the analyze step has failed for at least one test
                        for run in runs_successful:  # all successful runs (failed runs are already renamed)
                            if not run.analyze_successful:  # if 1 of N analyzes fails: rename
                                run.rename_failed()

                        # Don't remove when run fails
                        if not all(run.analyze_successful for run in runs_successful):  # don't delete build folder after all examples/runs
                            remove_build_when_successful = False

                        # Don't remove when (pre) external fails
                        for run in runs_successful:
                            for external in run.externals_pre:
                                if not all(externalrun.successful for externalrun in external.runs):  # don't delete build folder after all examples/runs
                                    remove_build_when_successful = False

                        # Don't remove when (post) external fails
                        for run in runs_successful:
                            for external in run.externals_post:
                                if not all(externalrun.successful for externalrun in external.runs):  # don't delete build folder after all examples/runs
                                    remove_build_when_successful = False

                    # 7.    perform analyze tests comparing corresponding runs from different commands
                    for iRun in range(len(example.command_lines[0].runs)):  # loop over runs of first command
                        # collect corresponding runs from different commands, i.e. cmd_*/run_0001, cmd_*/run_0002, ...
                        runs_corresponding = [command_line.runs[iRun] for command_line in example.command_lines]
                        for analyze in example.analyzes:
                            # perform only cross-command comparisons
                            if isinstance(analyze, Analyze_compare_across_commands):
                                print(tools.indent(tools.blue(str(analyze)), 2))
                                analyze.perform(runs_corresponding)
                                # Check if immediate stop is activated on failure
                                if args.stop and Analyze.total_errors > 0:
                                    s = tools.red('Stop on first error (-p, --stop) is activated! Analysis failed (cross-command comparisons)')
                                    print(s)
                                    sys.exit(1)

                # create coverage report for current build
                if args.coverage:
                    self.write_single_coverage_report(build, args)

                if remove_build_when_successful and not args.save:
                    tools.remove_folder(build.target_directory)
                print('=' * 132)

            # check if reggie is executed directly or via gitlab: if executed by hand combine the coverage data over all builds, gitlab uses the single reports and separate stage to combine
            if not self.coverage_env and args.coverage:
                self.combine_local_coverage_reports(args)

        # catch exception if bulding fails
        except BuildFailedException as ex:
            # print table with summary of errors
            summary.SummaryOfErrors(builds, args)

            # display error message
            print(tools.red(str(ex)))  # display error msg
            if hasattr(ex.build, 'cmake_cmd'):
                print(tools.indent(tools.yellow(str(" ".join(ex.build.cmake_cmd))), 1))
            if hasattr(ex.build, 'make_cmd'):
                print(tools.indent(tools.yellow(str(" ".join(ex.build.make_cmd))), 1))
            print(tools.indent("Build failed, see: " + str(ex.build.stdout_filename), 1))
            print(tools.indent("                   " + str(ex.build.stderr_filename), 1))
            print(tools.bcolors.RED)
            for line in ex.build.stderr[-20:]:
                print(tools.indent(line, 4), end=' ')  # skip linebreak
            print(tools.bcolors.ENDC)

            print("run 'reggie' with the command line option '-c/--carryon' to skip successful builds.")
            tools.finalize(start, 1, Run.total_errors, Analyze.total_errors, Analyze.total_infos)
            sys.exit(1)

    def perform_runs_in_parallel(self, build, example, args, num_processes):
        original_meshesdir = args.meshesdir
        try:
            # overwrite sys.stdout with local class to allow thread local print statements
            sys.stdout = ThreadSafeStdout()
            if args.coverage:
                s = tools.red('Code coverage in combination with parallel execution not tested yet!')
                print(tools.indent(s, 1))

            runs_to_process = list(self.command_line.runs)
            start_idx = 1
            if args.meshesdir:
                # OPTIMIZE: only perform the first run separatly, if externals are actually used. Otherwise all can be performed in parallel directly
                #     Problem: at this point the externals are "not known" (especially which external belongs to the current run and if external is building a mesh)
                #              so does the speed up justify the lookup of externals here?
                if len(runs_to_process) > num_processes:
                    # only perform first run separatley if remaining runs exceed number of processes, otherwise just run the mesh generation for each process, since this will be faster if all are possible in parallel
                    # OPTIMIZE: this is unfortunate for very short runs with a large number of externals (e.g. conv test, where 7 runs are performed and therefore 7 meshes created)
                    # -> it probably would still be faster the execute the first separately to get the 7 meshes)
                    # Problem: it would need knowledge off the number of externalruns and also how expensive they are
                    s = tools.yellow('Executing first run separately to create meshes only once!')
                    print(tools.indent(s, 1))
                    first_run = runs_to_process.pop(0)
                    self.perform_single_run(build, example, args, (1, len(self.command_line.runs), first_run), run_in_parallel=False)
                    start_idx = 2
                else:
                    s = tools.yellow(f'Ignoring meshesdir(={args.meshesdir}), since nunber of runs(={len(runs_to_process)}) is smaller than number of cores={getMaxCPUCores()}')
                    print(tools.indent(s, 1))
                    args.meshesdir = False

            # format function and arguments to pass to pool
            run_func = partial(self.perform_single_run, build, example, args, run_in_parallel=True)
            run_args = ((i, len(self.command_line.runs), r) for i, r in enumerate(runs_to_process, start=start_idx))
            with ThreadPool(processes=num_processes) as pool:
                for run_results in pool.imap_unordered(run_func, run_args):
                    print(run_results['reggie_stdout'], end='', flush=True)

        except Exception as e:
            print(e)
        finally:
            # if args.meshesdir was set to False for the current command line (since #runs<#Cores), reset it to the original value for the next command line
            args.meshesdir = original_meshesdir

    def perform_single_run(self, build, example, args, run_data, run_in_parallel=False):
        '''
        This function executes a single run for either the serial or parallel case.

        To maintain functionality for serial execution, new features or states should be
        stored as instance attributes (e.g., `run.successful`), which are later read by
        modules like summary.py.

        The thread based parallelization allows the main python process to collect all
        attributes (which would not be possible for a process pool).
        '''

        # Capture standard output during parallel execution to prevent print statements from overlapping
        if run_in_parallel:
            ThreadStdout = io.StringIO()
            sys.stdout.local.stream = ThreadStdout

        try:
            RunCount, NbrOfRuns, run = run_data
            # collect different runtimes (from externals and main run)
            run.externals_time = 0
            self.RunCount = RunCount
            print(tools.indent(f'Run {RunCount} of {NbrOfRuns}', 1))
            # log.info(str(run))
            # Database linking
            if self.database_path is not None and os.path.exists(run.target_directory):
                _, tail = os.path.split(self.database_path)
                os.symlink(self.database_path, os.path.join(run.target_directory, tail))
                print(tools.indent(tools.green(f'Preprocessing: Linked database [{self.database_path}] to [{run.target_directory}] ... '), 2))
            # CVAE scattering linking
            if self.cvae_scattering_cvae is not None and os.path.exists(run.target_directory):
                _, tail = os.path.split(self.cvae_scattering_cvae)
                os.symlink(self.cvae_scattering_cvae, os.path.join(run.target_directory, tail))
                print(tools.indent(tools.green(f'Preprocessing: Linked CVAE scattering cvae file [{self.cvae_scattering_cvae}] to [{run.target_directory}] ... '), 2))

            # 4.1 read the external options in 'externals.ini' within each example directory (e.g. eos, hopr, posti)
            #     distinguish between pre- and post processing
            if args.dry_run:
                run.externals_pre, run.externals_post, run.externals_errors = [], [], []
            else:
                run.externals_pre, run.externals_post, run.externals_errors = getExternals(os.path.join(run.source_directory, 'externals.ini'), run, build)

            # (pre) externals: loop over all externals available in external.ini
            external_failed = False
            if run.externals_pre is None:
                PreprocessingActive = False
            else:
                if len(run.externals_pre) == 0:
                    PreprocessingActive = False
                else:
                    PreprocessingActive = True
                    externalbinaries = [external.parameters.get("externalbinary") for external in run.externals_pre]
                    print(tools.indent(tools.green(f'Preprocessing: Started  {externalbinaries} pre-externals'), 3))

            if PreprocessingActive:
                for self.external_count, external in enumerate(run.externals_pre):
                    # use |= (bitwise or assignment) to check save if at least one external has failed
                    external_failed |= self.perform_external(external, run, build, args, stage='pre')
                print(tools.indent(tools.green(f'Preprocessing: Externals {externalbinaries} finished!'), 3))
                if not run_in_parallel:
                    # mesh creation only possible for single core runs, so keep track if all meshes are created or still needs to be done
                    # this is only checked after all externals have been executed for the current run, since its possible to create more than one mesh in a single run (e.g. convergence tests)
                    self.mesh_externals_done = True

            # 4.2    execute the binary file for one combination of parameters
            if args.dry_run:
                print(tools.indent(tools.yellow('dry-run: skipping execution'), 2))
            else:
                run.execute(build, self.command_line, args, external_failed)
                if not run.successful:
                    Run.total_errors += 1  # add error if run fails
                    # Check if immediate stop is activated on failure
                    if args.stop:
                        s = tools.red('Stop on first error (-p, --stop) is activated! Execution of run failed')
                        print(s)
                        sys.exit(1)

            # (post) externals: loop over all externals available in external.ini
            if run.externals_post is None:
                PostprocessingActive = False
            else:
                if len(run.externals_post) == 0:
                    PostprocessingActive = False
                else:
                    PostprocessingActive = True
                    externalbinaries = [external.parameters.get("externalbinary") for external in run.externals_post]
                    print(tools.indent(tools.green(f'Postprocessing: Started  {externalbinaries} post-externals'), 3))

            if PostprocessingActive:
                for self.external_count, external in enumerate(run.externals_post):
                    self.perform_external(external, run, build, args, stage='post')
                print(tools.indent(tools.green(f'Postprocessing: Externals {externalbinaries} finished!'), 3))

            # 4.3 Remove unwanted files: run analysis directly after each run (as opposed to the normal analysis which is used for analyzing the created output)
            for analyze in example.analyzes:
                if isinstance(analyze, Clean_up_files):
                    analyze.execute(run)

            if run_in_parallel:
                stdout_content = ThreadStdout.getvalue()
                ThreadStdout.close()
                return {'reggie_stdout': stdout_content}

        # delete thread level std to ensure correct print statements even if runs fails
        finally:
            if hasattr(sys.stdout, 'local') and hasattr(sys.stdout.local, 'stream'):
                del sys.stdout.local.stream

    def perform_external(self, external, run, build, args, stage='pre'):
        external_failed = False
        # (1): get the path and the parameterfiles to the external
        externaldirectory = external.parameters.get("externaldirectory")
        if externaldirectory.endswith('.ini'):
            external.directory = run.target_directory
            external.parameterfiles = [externaldirectory]
        else:
            external.directory = os.path.join(run.target_directory, externaldirectory)
            external.parameterfiles = [i for i in os.listdir(external.directory) if i.endswith('.ini')]

        externalbinary = external.parameters.get("externalbinary")

        # (2): loop over all parameterfiles available for the external
        for self.externalparameterfile_count, external.parameterfile in enumerate(external.parameterfiles):  # noqa: B020
            # (2.1): consider combinations
            external.runs = getExternalRuns(os.path.join(external.directory, external.parameterfile), external)

            # (3): loop over all combinations and parameterfiles for the external
            for self.externalrun_count, externalrun in enumerate(external.runs, start=1):
                # (3.1): run the external binary, use |= (bitwise or assignment) to check save if at least one external has failed
                external_failed |= self.perform_externalrun(externalrun, args, run, external, build, externalbinary, stage=stage)

        return external_failed

    def perform_externalrun(self, externalrun, args, run, external, build, externalbinary, stage='pre'):
        # check if meshes should be reused with symbolic links for each command line of example (only for pre stage)
        if stage == 'pre' and args.meshesdir:
            # check if externalbinary is set in self.MeshGeneration and should be executed only once
            self.Matched_Generator_Name = next((Generator_Name for Generator_Name in self.MeshGeneration if Generator_Name in externalbinary), None)
            externalcmd = self.mesh_external(run, external, externalrun, build, args) if self.Matched_Generator_Name else externalrun.execute(build, external, args)
        else:
            externalcmd = externalrun.execute(build, external, args)

        external_run_failed = False
        if not externalrun.successful:
            external_run_failed = True
            s = tools.red(f'Execution ({stage}) external failed: {externalcmd}')
            run.externals_errors.append(s)
            ExternalRun.total_errors += 1  # add error if externalrun fails
            # Check if immediate stop is activated on failure
            if args.stop:
                s = tools.red(f'Stop on first error (-p, --stop) is activated! Execution ({stage}) external failed')
                print(s)
                sys.exit(1)

        # add external runtime
        run.externals_time += externalrun.walltime
        return external_run_failed
