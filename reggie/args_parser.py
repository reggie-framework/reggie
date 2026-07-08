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
import argparse
import os
from sys import platform, exit
from pathlib import Path
import socket
import re
import subprocess
from reggie import tools
from reggie import check
from reggie.outputdirectory import OutputDirectory

# try:
#     # Python 2.7
#     import commands
# except Exception:
#     pass


def getMaxCPUCores():
    """
    Get the total number of available physical cores, ignore hyper threading or SMT etc.

    The affinity is ignored at the moment, e.g., if the total number of processes is artificially limited,
    see https://docs.python.org/3/library/os.html#os.sched_getaffinity.
    """
    # Docker
    # > Check cpuset.cpus for Docker-specific CPU limits
    try:
        line = Path('/sys/fs/cgroup/cpuset.cpus').read_text().strip()
        cnt = 0

        # Assemble the number from the comma-separated list
        for prt in line.split(','):
            if '-' in prt:
                start, end = map(int, prt.split('-'))
                cnt += end - start + 1
            else:
                cnt += 1

        if cnt > 0:
            return cnt
    except Exception:
        pass

    # Linux
    # > Parse /proc/cpuinfo for physical cores
    try:
        physical_cores = {}
        physical_id = None
        with open('/proc/cpuinfo') as file:
            for line in file:
                if line.strip():
                    val = line.split(':')
                    key = val[0].strip()
                    value = val[1].strip() if len(val) > 1 else None

                    if value is None:
                        continue

                    # Look for "physical id" (CPU socket) and "core id" (core within socket)
                    if key == 'physical id':
                        physical_id = int(value)
                    elif key == 'core id':
                        core_id = int(value)

                        # physical id has to come before core id
                        if physical_id is None:
                            raise IndexError

                        physical_cores[(physical_id, core_id)] = True

        # Count unique (physical_id, core_id) pairs
        # print(len(physical_cores))
        return len(physical_cores)
    except Exception:
        pass

    # Python 2.6+
    # > POSIX fallback: Use os.sched_getaffinity to get available cores
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        pass

    # If all else fails, return 0 (unknown physical cores)
    return 0


def getArgsAndBuilds():
    """Get command line arguments and builds in check directory from 'builds.ini'"""
    # fmt: off
    parser = argparse.ArgumentParser(description='DESCRIPTION:\nRegression checker for NRG codes.\nSupply the path to a /regressioncheck/checks/ directory within a repository containing a CMakeLists.txt file which can automatically be build using cmake. ', formatter_class=argparse.RawTextHelpFormatter)  # noqa: E501 Line too long
    parser.add_argument('-v', '--version', action='version', version='') # print nothing since the version is displayed anyway when reggie is called
    parser.add_argument('-c', '--carryon', action='store_true', help='''Continue build/run process.
      --carryon         : build non-existing binary-combinations and run all examples for thoses builds
      --carryon --run   : run all failed examples''')
    parser.add_argument('-e', '--exe'        , help='Path to executable of code that should be tested.')
    parser.add_argument('-m', '--MPIexe'     , help='Path to mpirun executable. The correct MPI lib must be used, i.e. the one which which the executable (e.g. flexi) was compiled, e.g., /opt/openmpi/2.0.2/bin/mpirun.', default='mpirun')  # noqa: E501
    parser.add_argument('-d', '--debug'      , help='Debug level.', type=int, default=0)
    parser.add_argument('-j', '--buildprocs' , help='Number of processors used for compiling (make -j XXX).', type=int, default=0)
    parser.add_argument('-b', '--basedir'    , help='Path to basedir of code that should be tested (contains CMakeLists.txt).')
    parser.add_argument('-y', '--dummy'      , help='Use dummy_basedir and dummy_checks for fast testing on dummy code.', action='store_true')
    parser.add_argument('-n', '--singledir'  , help='Use a single build directory for all combinations', action='store_true')
    parser.add_argument('-r', '--run'        , help='Run all binaries for all examples with all run-combinations for all existing binaries.', action='store_true' )
    parser.add_argument('-s', '--save'       , help='Do not remove output directories buildsXXXX in output_dir after successful run.', action='store_true')
    parser.add_argument('-t', '--compiletype', help='Override all CMAKE_BUILD_TYPE settings by ignoring the value set in builds.ini (e.g. DEBUG or RELEASE).')
    parser.add_argument('-a', '--hlrs'       , help='Run on with aprun (24-core hlrs system).', action='store_true')
    parser.add_argument('-z', '--rc'         , help='Create/Replace reference files that are required for analysis. After running the program, the output files are stored in the check-/example-directory.', action='store_true', dest='referencescopy')        # noqa: E501
    parser.add_argument('-f', '--fc'         , help='Create/Replace required restart files (if defined in command_line.ini). After running the program, the output files are stored in the check-/example-directory.', action='store_true', dest='restartcopy')  # noqa: E501
    parser.add_argument('-i', '--noMPI'      , help='Run program without "mpirun" (single thread execution).', action='store_true')
    parser.add_argument('-p', '--stop'       , help='Stop on first error.', action='store_true')
    parser.add_argument('-l', '--limitprocs' , help='Limit the number of processes to be used for the rune.', type=int, default=0)
    parser.add_argument('check', help='Path to check-/example-directory.')
    parser.add_argument('-o', '--coverage'   , help='Compile code with code coverage option, always returns output in json format. Additional values (resulting in additional output formats): 1=HTML output, 2=Cobertura XML, also allows 12 for both. Default=0 if flag used without value.', nargs='?', const='0', default=None) # noqa: E501
    parser.add_argument('--gcovr_extra'      , help='Extra arguments (string) to pass to gcovr (e.g. --exclude-lines-by-pattern <pattern> or --include-internal-functions). Additional arguments can be obtained from the gcovr documentation.', default=None) # noqa: E501
    parser.add_argument('--meshesdir'        , help='Deactivate re-using meshes created with hopr/pyhope as external. With this flag all externals are pre-executed before each run, even if they create the same mesh all the time. Otherwise hopr/pyhope is only run once for each example and meshes are stored in separate directory to use with symbolic links.', action='store_false') # noqa: E501
    parser.add_argument('--gitlab-ci'        , help='Activated automatically when running gitlab-ci pipelines via environment variable REGGIE_GITLAB_CI to print Running [...] + Successful/Failed [x.xx sec] in a single line instead of breaking the last part into a new line.', action='store_true')  # noqa: E501
    parser.add_argument('--dry-run'          , help='Skip building and execution, only perform analyze functions on existing output directories.', action='store_true', dest='dry_run')
    # fmt: on
    # parser.set_defaults(carryon=False)
    # parser.set_defaults(dummy=False)
    # parser.set_defaults(run=False)
    # parser.set_defaults(save=False)
    # parser.set_defaults(hlrs=False)
    # parser.set_defaults(referencescopy=False)
    # parser.set_defaults(restartcopy=False)
    # parser.set_defaults(noMPI=False)

    # get reggie command line arguments
    args = parser.parse_args()

    # Set default values
    args.noMPIautomatic = False

    # Store ENV varaible if flag --gitlab-ci is set true via command line for later access in externalcommand
    if args.gitlab_ci:
        os.environ["REGGIE_GITLAB_CI"] = "1"

    # Check ENV variable for args.gitlab_ci
    gitlab_ci_env = os.getenv('REGGIE_GITLAB_CI')
    if gitlab_ci_env:
        args.gitlab_ci = True

    # ENV variable for gcovr_extra
    gcovr_args = os.getenv('GCOVR_ARGS')
    if gcovr_args:
        if args.gcovr_extra:
            print(f'Overwriting local additional gcovr arguments {args.gcovr_extra} with arguments from environment variable {gcovr_args}!')
        args.gcovr_extra = gcovr_args

    # Check OS
    if re.search('^linux', platform):
        hostname = socket.gethostname()
        print(f"platform: {platform}, hostname: {hostname}")
        if re.search('^mom[0-9]+$', hostname):
            print(tools.yellow('Automatic detection of hlrs system: Assuming aprun is used and setting args.hlrs = True'))
            args.hlrs = True
        elif re.search('^eslogin[0-9]+$', hostname):
            if args.hlrs:
                raise Exception('Running with -a or --hlrs. Cannot run this program on a login node. Get interactive job and run on mom node!')

    # setup basedir
    if args.dummy:
        # For testing reggie during reggie-developement:
        # Overwrite basedir and check directory with dummy directories.
        reggieDir = os.path.dirname(os.path.realpath(__file__))
        args.basedir = os.path.join(reggieDir, 'dummy_basedir')
        args.check = os.path.join(reggieDir, 'dummy_checks/test')
        print(f"Basedir directory switched to '{args.basedir}'")
        print(f"Check   directory switched to '{args.check}'")
    else:
        # For real reggie-execution:
        # Setup basedir (containing CMakeLists.txt) by searching upward from current working directory
        if args.basedir is None:  # start with current working directory
            args.basedir = os.getcwd()
        try:
            if args.exe is None and not args.dry_run:  # only get basedir if no executable is supplied and not in dry-run mode
                args.basedir = tools.find_basedir(args.basedir)
        except Exception:
            print(tools.red("Basedir (containing 'CMakeLists.txt') not found!\nEither specify the basedir on the command line or execute reggie within a project with a 'CMakeLists.txt'."))
            exit(1)

        # Check if directory exists
        if not os.path.exists(args.check):
            print(tools.red(f"Check directory not found: '{args.check}'"))
            exit(1)
        else:
            # Check if file or link path was supplied
            if os.path.isfile(args.check):
                print(tools.red(f"Check directory supplied is a file: '{args.check}'. Please supply a directory path"))
                exit(1)
            # Check if directory path was supplied
            elif os.path.isdir(args.check):
                pass
            # Check rest
            else:
                print(tools.red(f"Check directory supplied is not a directory path: '{args.check}'. Please supply a directory path"))
                exit(1)

    # delete the building directory when [carryon = False] and [run = False] before getBuilds is called
    if not args.carryon and not args.run and not args.dry_run:
        tools.remove_folder(OutputDirectory.output_dir)

    # get builds from checks directory if no executable is supplied
    if args.exe is None:  # if not exe is supplied, get builds
        # read build combinations from checks/XX/builds.ini, or fall back to a dummy standalone when dry-running a direct example directory
        builds = [check.Standalone('__dry_run__', args.check)] if args.dry_run else check.getBuilds(args.basedir, args.check, args.compiletype, args.singledir, args.coverage)
    else:
        if not os.path.exists(args.exe):  # check if executable exists
            print(tools.red(f"No executable found under '{args.exe}'"))
            exit(1)
        else:
            builds = [check.Standalone(args.exe, args.check)]  # set builds list to contain only the supplied executable
            args.noMPIautomatic = check.StandaloneAutomaticMPIDetection(args.exe)  # Check possibly existing userblock.txt to find out if the executable was compiled with MPI=ON or MPI=OFF
            args.run = True  # set 'run-mode' do not compile the code
            args.basedir = None  # since code will not be compiled, the basedir is not required

    if args.dry_run:
        args.run = True  # skip building in dry-run mode

    # Try to detect MPICH
    args.detectedMPICH = False
    try:
        if args.MPIexe == 'mpirun':
            status, result = subprocess.getstatusoutput(f"{args.MPIexe} -h | grep -i mpich")
            # try:
            #     status, result = subprocess.getstatusoutput("{} -h | grep -i mpich".format(args.MPIexe))
            # except Exception:
            #     # Fallback for python2.7
            #     status, result = commands.getstatusoutput("{} -h | grep -i mpich".format(args.MPIexe))
            if len(result) > 0 and status == 0:
                args.detectedMPICH = True
    except Exception:
        pass

    args.MaxCores = args.limitprocs  # set the maximum number of processes to be used for the run
    # Set maximum number of processes/cores for mpich as over-subscription results in a massive performance drop
    if args.detectedMPICH:
        args.MaxCores = getMaxCPUCores()
        print(tools.yellow(f'WARNING: MPICH detected, which limits the total number of processes that can be used to {args.MaxCores} as over-subscription results in a massive performance drop'))

    if args.run:
        print("args.run -> skip building")
        # in 'run-mode' remove all build from list of builds if their binaries do not exist (build.binary_exists() == False)
        # skip binary existence check for dry-run since no binary is needed
        if not args.dry_run:
            builds = [build for build in builds if build.binary_exists()]

    if len(builds) == 0:
        print(tools.red("List of 'builds' is empty! Maybe switch off '--run'."))
        exit(1)

    # display all command line arguments
    print("Running with the following command line options")
    for arg in list(args.__dict__):
        print(arg.ljust(15) + " = [ " + str(getattr(args, arg)) + " ]")
    print('=' * 132)

    return args, builds
