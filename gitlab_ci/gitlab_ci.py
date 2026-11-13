# ==================================================================================================================================
# Copyright (c) 2017 - 2018 Stephen Copplestone
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
import re
import logging
import argparse
import shutil
import tools  # ty:ignore[import-not-found]
import gitlab_ci_tools  # ty:ignore[import-not-found]

# import reggie source code
# use reggie functions by adding the path
import settings  # ty:ignore[import-not-found]

settings.init()  # Call only once

sys.path.append(settings.absolute_reggie_path)
reggie_exe_path = os.path.join(settings.absolute_reggie_path, 'reggie.py')
if not os.path.exists(reggie_exe_path):
    print(f"Reggie main file not found in reggie repository under: '{reggie_exe_path}'")
    sys.exit(1)


def CheckBinaryCall(c):
    if c.find("-e") >= 0:  # find lines which contain "-e"
        c = c[: c.find("-e")]  # remove everything after "-e"

        b = c[c.find("/regressioncheck/checks") + 24 :].strip()  # cut away name before /regressioncheck/checks/
        if b.find("/") >= 0:  # find lines which contain "/"
            print("  " + c, end=' ')  # skip linebreak
            remove_string = b[b.find("/") :]
            print(" (Removed " + tools.red(remove_string) + " and '-e' binary call)")
            b = b[: b.find("/")]  # remove everything after "/"
            case_dir = os.path.join(basedir, 'regressioncheck')
            case_dir = os.path.join(case_dir, 'checks')
            case_dir = os.path.join(case_dir, b)
            if not os.path.exists(case_dir):  # Sanity check if folder exists: use only the part of the string up to the first (whitespace (" ")
                print(tools.red(f"case directory not found under: '{case_dir}'"))
                sys.exit(1)
            c = c[: c.find(remove_string)].strip()  # remove everything after remove_string
        else:
            print("  " + c, end=' ')  # skip linebreak
            print(" (Removed '-e' binary call)")

    return c.strip()


def DisplayInitMessage(Bool, Message):
    if Bool:
        print(f"\n{Message}\n")
        Bool = False

    return Bool


"""
General workflow:
1.  get the command line arguments 'args' with path to ".gitlab-ci.yml" file
2.  FIX THIS: set the logger 'log' with the debug level from 'args' to determine the level of logging which displays output to the user
3.  FIX THIS: perform the regression check by a) building executables
                                    b) running the code
                                    c) performing the defined analyzes
4.  FIX THIS: display the summary table with information for each build, run and analysis step
5.  FIX THIS: display if regression check was successful or not and return the corresponding error code
"""

print(132 * '=' + "\n" + "gitlab-ci processing tool, add nice ASCII art here" + "\n" + 132 * '=')
start = timer()

# fmt: off
# argument parser
parser = argparse.ArgumentParser(description='DESCRIPTION:\nScript for executing the regression checker for NRG codes multiple times with information from a gitlab-ci.yml runner file (the relevant python calls will be extracted).\nSupply the path to the gitlab-ci.yml of the repository that also contains a /regressioncheck/checks structure supporting reggie and multiple tests can automatically be performed.\nThe output will be stored in the top repository directory under /output_dir_gitlab_tool/.', formatter_class=argparse.RawTextHelpFormatter) # noqa: E501 line too long
parser.add_argument('gitlab_ci', help='Path to gitlab-ci.yml which also contains a /regressioncheck/checks/... structure')
parser.add_argument('-s', '--stage', default='full', help='Gitlab-ci execution stage: Supply DO_NIGHTLY, DO_WEEKLY, DO_CHECKIN, etc. flag for extracting the relevant cases from gitlab-ci.yml. Default executes all stages.') # noqa: E501
parser.add_argument('-b', '--begin', type=int, default=1,  help='Number of the case: where to start with the run (from the list that this tools creates)')
parser.add_argument('-d', '--debug', type=int, default=0, help='Debug level for this program. Dumps all info to the screen.')
parser.add_argument('-i', '--info', type=int, default=1, help='Debug level for the subsequent program execution (e.g. flexi).')
parser.add_argument('-o', '--only', action='store_true',help='Only run one case and exit afterwards (from the list that this tools creates).')
parser.add_argument('-n', '--dryrun', action='store_true',help='Simply list all possible cases without performing any run.')
parser.add_argument('-t', '--compiletype', help='Override all CMAKE_BUILD_TYPE settings by ignoring the value set in builds.ini (e.g. DEBUG or RELEASE).')
# fmt: on

# get reggie command line arguments
args = parser.parse_args()

# set the logger 'log' with the debug level from 'args' to determine the level of logging which displays output to the user
tools.setup_logger(args.debug)
log = logging.getLogger('logger')

# check if file exists
if os.path.isdir(args.gitlab_ci):
    print(tools.yellow(f"Supplied path is [{args.gitlab_ci}]. Searching for '.gitlab-ci.yml' there."))
    args.gitlab_ci = os.path.join(args.gitlab_ci, '.gitlab-ci.yml')
if not os.path.exists(args.gitlab_ci):
    print(tools.red(f"gitlab-ci.yml file not found under: '{args.gitlab_ci}'"))
    sys.exit(1)

# display all command line arguments
print("Running with the following command line options")
for arg in args.__dict__:
    print(f"{arg.ljust(15)} = [ {getattr(args, arg)} ]")
print('=' * 132)


# set the basedir (where the code is) and the reggiedir (where the reggie.py is)
basedir = os.path.abspath(os.path.dirname(args.gitlab_ci))
reggiedir = os.path.abspath(os.path.dirname(reggie_exe_path))

print(tools.blue("Using code under      [basedir]: " + str(basedir)))
print(tools.blue("Using reggie under  [reggiedir]: " + str(reggiedir)))
print(tools.blue("Running checks for [args.stage]: " + str(args.stage)))
print('=' * 132)

reggie_path = os.path.join(reggiedir, 'reggie.py')
if not os.path.exists(reggie_path):  # check if file exists
    print(tools.red(f"reggie not found in reggie directory: '{reggie_path}'"))
    sys.exit(1)

cases = []
commands = []
firstCheckInExample = True
firstConditionalExample = True
with open(args.gitlab_ci, 'r') as f:  # read file as "f"
    for line in f:  # read every line
        s = str(line.strip())  # remove leading and trailing whitespaces

        # 1.  Skip comments
        if s.find("#") == 0:
            continue

        # 2. Check for conditional (nightly, weekly) runs, by finding lines which contain "if"
        if s.find("if") >= 0:
            if re.search(r'\[(.*?)\]', s):  # find lines with "[....]" in it, meaning opening "[" and closing "]" parenthesis
                # Check stage only if user supplies one
                if args.stage != 'full' and s.lower().find(args.stage.lower()) == -1:  # Skip stages that do not correspond to the user supplied stage
                    continue

                if s.find("python") >= 0:  # find lines which contain "python"
                    c = s[s.find("python") :]  # create string "c" starting at "python"
                    if c.find(";") >= 0:  # find lines which contain ";"
                        c = c[: c.find(";")]  # remove everything after ";"

                    # Display Init Information
                    firstConditionalExample = DisplayInitMessage(firstConditionalExample, 'Conditional examples: Removing reggie calls with supplied binary (it must be built from scratch here)')

                    # Remove possible calls with binaries
                    c = CheckBinaryCall(c)

                    # Add the new command line only if it is unique
                    if c not in commands:
                        commands.append(c)  # add command line to list
                        cases.append(gitlab_ci_tools.Case(c))  # and the case to the list of cases

        # 3. Check other runs (generally "CHECKIN" examples)
        else:
            # Check stage only if user supplies one
            if args.stage == 'full' or args.stage.lower() == 'do_checkin' and s.find("python") >= 0:  # find lines which contain "python"
                c = s[s.find("python") :]  # create string "c" starting at "python"

                # Display Init Information
                firstCheckInExample = DisplayInitMessage(firstCheckInExample, 'CHECKIN examples: Removing reggie calls with supplied binary (it must be built from scratch here)')

                # Remove possible calls with binaries
                c = CheckBinaryCall(c)

                # Add the new command line only if it is unique
                if c not in commands:
                    commands.append(c)  # add command line to list
                    cases.append(gitlab_ci_tools.Case(c))  # and the case to the list of cases


print(132 * '=')

if not args.dryrun:  # do not execute anythin in dryrun mode
    # switch to basedir+/output_dir_gitlab_tool
    target_directory = os.path.join(basedir, 'output_dir_gitlab_tool')
    shutil.rmtree(target_directory, ignore_errors=True)
    tools.create_folder(target_directory)
    os.chdir(target_directory)
    print(f"Creating output under {target_directory}")
else:
    print("List of possible cases from gitlab-ci.yml are")

print(" ")
nErrors = 0
for i, case in enumerate(cases):
    # extract the reggie case from the command in the gitlay-ci.yml line by looking for "reggie.py" and "/regressioncheck/checks"
    c = case.command[case.command.find("reggie.py") + 9 :].strip()
    c = c[c.find("/regressioncheck/checks") :].strip()
    c = str(basedir + c).strip()  # add basedir to reggie-checks folder
    case_dir = c.split(" ")[0]
    if not os.path.exists(case_dir):  # Sanity check if folder exists: use only the part of the string up to the first (whitespace (" ")
        print(tools.red(f"case directory not found under: '{case_dir}'"))
        sys.exit(1)

    # set the command line "cmd"
    cmd = ["python", reggie_path] + [str(x).strip() for x in c.split(" ")]

    # add debug level to gitlab-ci command line
    if args.info:
        cmd.append("-d2")

    # add compiletype if supplied
    if args.compiletype:
        cmd.extend(("-t", args.compiletype))

    cmd_string = " ".join(cmd)
    # cmd = ["ls","-l"] # for testing some other commands

    if args.dryrun:  # do not execute anythin in dryrun mode
        print(str(f"[{i + 1:5d}] ") + cmd_string)
    else:
        # run case depending on supplied (or default) number "begin"
        if i + 1 >= args.begin:  # run this case
            s_Color = "f[{i+1:5d}]" + tools.blue(" Running  ") + cmd_string + " ..."
            s_NoColor = str(f"[{i + 1:5d}]") + " Running  " + cmd_string + " ..."
            print(s_Color)
            # print(s+" ...", end=' ') # skip linebreak
            # print(s+" ...") # skip linebreak
            # print(s+" ...", end='\r') # skip linebreak
            # print(s+" ...\r") # skip linebreak
            # log.debug(s+" ...") # skip linebreak
            # ncols=len(s)
            # print(s+f"\033[F\033[{ncols}G Space-lead appended text", end=' ')
            # print(str("[%5d]" % i)+tools.blue(" Running  ")+cmd_string, end='\r') # skip linebreak

            # print(s, end='\r') # skip linebreak
            # When debug output is desired, break line here in order for debugging output to beging in the following line from execute_cmd method
            if args.debug > 0:
                print(" ")

            # run the code and generate output
            try:
                if case.execute_cmd(cmd, target_directory, string_info=s_NoColor.strip()) != 0:  # use uncolored string for cmake
                    case.failed = True
            except Exception:  # this fails, if the supplied command line is corrupted
                print(tools.red("Failed"))
                case.failed = True

            # if case fails, add error to number of errors
            if case.failed:
                nErrors += 1

            # move the std.out file
            old_std = os.path.join(target_directory, 'std.out')
            new_std = os.path.join(target_directory, f'std-{i + 1}.out')
            if os.path.exists(os.path.abspath(old_std)):  # check if file exists
                os.rename(old_std, new_std)

            # move the err.out file
            old_err = os.path.join(target_directory, 'std.err')
            new_err = os.path.join(target_directory, f'std-{i + 1}.err')
            if os.path.exists(os.path.abspath(old_err)):  # check if file exists
                os.rename(old_err, new_err)

            # exit, if user wants to
            if args.only:  # if only one case is to be run -> exit(0)
                print(" ")
                gitlab_ci_tools.finalize(start, nErrors)
                sys.exit(0)
        else:  # skip this case
            print("[f{i+1:5d}]" + tools.yellow(" Skipping ") + cmd_string)

print(" ")
gitlab_ci_tools.finalize(start, nErrors)
