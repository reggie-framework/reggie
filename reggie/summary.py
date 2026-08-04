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
from timeit import default_timer as timer
import os
import sys
import collections

from reggie import check
from reggie import tools
from reggie.outputdirectory import OutputDirectory
from itertools import starmap


def StartsWithCMD(pathSplit, iDir):
    try:
        if pathSplit[iDir + 1].startswith('cmd_'):
            return True
    except Exception:
        pass

    return False


def SummaryOfErrors(builds, args):
    """
    Display a summary table with information for each build, run and analyze

    General workflow:
    1. loop over all builds, examples, command_lines, runs and for every run set the output strings
       and get the maximal lengths of those strings
    2. print header
    3. loop over all builds
    3.1  print some information of the build
    3.2  within each build loop over all examples, command_lines, runs and for every run print some information:
    3.2.1  print an empty separation line if number of MPI threads changes
    3.2.2  print (only if changes) a line with all run parameters except the inner most, which is printed in 3.2.3
    3.2.3  print a line with following information:
             run.globalnumber, run.parameters[0] (the one not printed in 3.2.2), run.target_directory, MPI, run.walltime, run.result
    3.2.4  print the analyze results line by line
    """
    # fmt: off
    param_str_old    = ""
    str_MPI_old      = "-"
    restart_file_old = "-"
    # fmt: on

    # 1. loop over all runs and set output strings
    max_lens = collections.OrderedDict([("#run", 4), ("options", 7), ("path", 4), ("MPI", 3), ("time", 4), ("ext", 3), ("Info", 4)])
    for build in builds:
        for example in build.examples:
            for command_line in example.command_lines:
                for run in command_line.runs:
                    # fmt: off
                    run.output_strings            = {}
                    run.output_strings['#run']    = str(run.globalnumber)
                    run.output_strings['options'] = ""
                    # fmt: on
                    # Check number of variations in parameter list(run.digits.items())[0][1]
                    restart_file = command_line.parameters.get('restart_file', None)
                    run.restart_file_used = False
                    if next(iter(run.digits.items()))[1] > 0:
                        run.output_strings['options'] += "{}={}".format(*next(iter(run.parameters.items())))  # print parameter and value as [parameter]=[value]
                    elif restart_file:  # if no parameter is varied, check if the restart file is used
                        run.restart_file_used = True
                        if restart_file != restart_file_old:  # only display once
                            run.output_strings['options'] += "{}={}".format('restart_file', restart_file)  # print parameter and value as [parameter]=[value]
                            restart_file_old = restart_file

                    # fmt: off
                    run.output_strings['path']    = os.path.relpath(run.target_directory,OutputDirectory.output_dir)
                    run.output_strings['MPI']     = command_line.parameters.get('MPI', '-')
                    run.output_strings['time']    = f"{run.walltime:2.1f}"
                    run.output_strings['ext']     = f"{run.externals_time:2.1f}"
                    run.output_strings['Info']    = run.result
                    run.outputMPIyellow = False
                    # fmt: on

                    # get the max lens before adding ansi color escape codes (which changes the string length)
                    for key in run.output_strings:
                        max_lens[key] = max(max_lens[key], len(run.output_strings[key]))  # set max column widths for summary table

                    # Coloured path name
                    try:
                        pathSplit = run.output_strings['path'].split('/')
                        pathColoured = ''
                        delimiter = ''
                        for iDir, iDirName in enumerate(pathSplit):
                            foundCMD = StartsWithCMD(pathSplit, iDir)
                            if foundCMD:
                                pathColoured += delimiter + f'{tools.pink(iDirName)}'
                            else:
                                pathColoured += delimiter + f'{iDirName}'
                            delimiter = '/'
                        run.path_coloured = pathColoured
                    except Exception:
                        pass
                    # Check if command_line.ini has MPI>1 but the binary is built with MPI=OFF and therefore executed in single mode
                    try:
                        if build.MPIrunDeactivated:
                            try:
                                cores = command_line.parameters.get('MPI', '-')
                                if int(cores) > 1:
                                    run.output_strings['MPI'] = f'{1} (changed from {run.output_strings["MPI"]})'
                                    run.outputMPIyellow = True
                            except Exception:
                                run.output_strings['MPI'] = f'{1} (changed from {run.output_strings["MPI"]})'
                                run.outputMPIyellow = True
                    except Exception:
                        pass

                    # Check if MaxCores was set and more than the number of physical cores
                    try:
                        try:
                            cores = command_line.parameters.get('MPI', '-')
                            # Check if the field 'MPI' can be converted to an integer and compare with MaxCores (usually MPICH is
                            # limited to the actual number of pyhsical processors)
                            if int(cores) > args.MaxCores and args.MaxCores > 0:
                                run.output_strings['MPI'] = f'{args.MaxCores} (changed from {run.output_strings["MPI"]})'
                                run.outputMPIyellow = True
                        except Exception:
                            # Check if the keyword 'reduced' is in the field 'MPI', which implies that the number of cores was
                            # reduced due to nProcs > nElems
                            if 'reduced' in run.output_strings["MPI"]:
                                run.output_strings['MPI'] = f'{run.output_strings["MPI"]}'
                                run.outputMPIyellow = True
                            else:
                                run.output_strings['MPI'] = f'{args.MaxCores} (changed from {run.output_strings["MPI"]})'
                                run.outputMPIyellow = True
                    except Exception:
                        pass

    # 2. print header
    print(132 * "=")
    print(" Summary of Errors" + "\n")
    spacing = 1
    for key, value in list(max_lens.items()):
        print(key.ljust(value), spacing * ' ', end=' ')  # skip linebreak
    print()

    # 3. loop over alls builds
    for build in builds:
        # 3.1 print cmake flags if no external binary was used for execution
        print('-' * 132)
        if isinstance(build, check.Standalone):
            print("Binary supplied externally under ", build.binary_path)
        elif isinstance(build, check.Build):
            print(f"Build {build.number:d} of {len(builds):d} ({build.result:s}) compiled with in [{build.walltime:.2f} sec]:")
            print(" ".join(build.cmake_cmd_color))
            if build.return_code != 0:  # stop output as soon as a failed build in encountered
                break

        # 3.2 loop over all examples, command_lines and runs
        for example in build.examples:
            for command_line in example.command_lines:
                for run in command_line.runs:
                    # 3.2.1 print separation line only if MPI threads change
                    if run.output_strings["MPI"] != str_MPI_old:
                        print()
                        str_MPI_old = run.output_strings["MPI"]
                    # 3.2.2 print the run parameters, except the inner most (this one is displayed in # 3.2.3)
                    paramsWithMultipleValues = [item for item in list(run.parameters.items())[1:] if run.digits[item[0]] > 0]
                    param_str = ", ".join(list(starmap("{}={}".format, paramsWithMultipleValues)))  # skip first index
                    restart_file = command_line.parameters.get('restart_file', None)
                    if not param_str_old.startswith(param_str) or len(param_str_old) == 0:  # Only print when the parameter set changes
                        if restart_file and not run.restart_file_used and restart_file != restart_file_old:  # Add restart file once
                            if len(param_str) > 0:
                                param_str += ", "
                            param_str += "{}={}".format('restart_file', restart_file)
                            restart_file_old = restart_file
                        if len(param_str) > 0:
                            print("".ljust(max_lens["#run"]), spacing * ' ', tools.yellow(param_str))

                    param_str_old = param_str

                    # 3.2.3 print all output_strings
                    for key, value in list(max_lens.items()):
                        # Print options with .ljust
                        if key == "options":
                            print(tools.yellow(run.output_strings[key].ljust(value)), end=' ')  # skip linebreak
                        elif key == "MPI" and any([args.noMPI, args.noMPIautomatic]):
                            print(tools.yellow("1".ljust(value)), end=' ')  # skip linebreak
                        elif key == "MPI" and run.outputMPIyellow:
                            print(tools.yellow(f'{run.output_strings[key].ljust(value)}'), end=' ')  # skip linebreak
                        elif key == "path" and hasattr(run, 'path_coloured'):
                            # use run.output_strings['path'] to get visible length of path for padding calculation, since ansi codes changes length of string
                            padding = max(0, value - len(run.output_strings[key]))
                            # print coloured string with correct padding from visual string
                            print(run.path_coloured + (' ' * padding), end=' ')
                        else:
                            print(run.output_strings[key].ljust(value), end=' ')  # skip linebreak
                        print(spacing * ' ', end=' ')  # skip linebreak
                    print()

                    # 3.2.4  print the analyze results line by line
                    for result in run.analyze_results:
                        print(tools.red(result).rjust(150))

                    # 3.2.5  print the external results line by line
                    for error in run.externals_errors:
                        print(tools.red(error).rjust(150))

                    # print an empty line after all errors were displayed
                    if len(run.analyze_results) > 0 or len(run.externals_errors):
                        print()


def finalize(start, build_errors, run_errors, external_run_errors, analyze_errors, analyze_infos):
    """Display if regression check was successful or not and return the corresponding error code"""
    if build_errors + run_errors + analyze_errors + analyze_infos + external_run_errors > 0:
        if run_errors + analyze_errors + external_run_errors > 0:
            print(tools.bcolors.RED + 132 * '=')
            print("reggie 2.0  FAILED!", end=' ')  # skip linebreak
            return_code = 1
        else:
            print(tools.bcolors.YELLOW + 132 * '=')
            print("reggie 2.0  COMPLETED!", end=' ')  # skip linebreak
            return_code = 1
    else:
        print(tools.bcolors.BLUE + 132 * '=')
        print("reggie 2.0  successful!", end=' ')  # skip linebreak
        return_code = 0

    if start > 0:  # only calculate run time and display output when start > 0
        # fmt: off
        end = timer()
        sec = end - start
        minutes , seconds = divmod(sec     , 60.0)
        hours   , minutes = divmod(minutes , 60.0)
        days    , hours   = divmod(hours   , 24.0)
        # fmt: on
        print(f"in [{sec:2.2f} sec] [ {int(days):02d}:{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d} ]")
    else:
        print()

    print(f"Number of build        errors: {build_errors:d}")
    print(f"Number of run          errors: {run_errors:d}")
    print(f"Number of external run errors: {external_run_errors:d}")
    print(f"Number of analyze      errors: {analyze_errors:d}")
    print(f"Number of analyze       infos: {analyze_infos:d}")

    print('=' * 132 + tools.bcolors.ENDC)
    sys.exit(return_code)
