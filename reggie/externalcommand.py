# ==================================================================================================================================
# Copyright (c) 2017 - 2026 Stephen Copplestone, Matthias Sonntag, and Leon Teichroeb
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
import glob
import subprocess
import logging
import threading
from queue import Queue
from timeit import default_timer as timer
from collections.abc import Iterable

from reggie import tools


def replace_wild_cards_recursive(cmd, workingDir):
    # Check each cmd list entry for a wild card and exchange this entry with the globbed items
    for i in enumerate(cmd):
        # Check for wild cards
        if "*" in i[1]:
            # fmt: off
            absolutePath     = os.path.join(workingDir, i[1])
            files            = sorted(glob.glob(absolutePath), key=lambda x: os.path.splitext(os.path.basename(x))[0])
            files            = [sub.replace(workingDir+'/', '') for sub in files]
            cmd[i[0]:i[0]+1] = files
            # call function recursively to replace multiple wild cards
            cmd = replace_wild_cards_recursive(cmd, workingDir)
            # fmt: on
    return cmd


class ExternalCommand:
    def __init__(self):
        self.stdout = []
        self.stderr = []
        self.stdout_filename = None
        self.stderr_filename = None
        self.return_code = 0
        self.result = ""
        self.walltime = 0

        # Check ENV variable for args.gitlab_ci, which is either set externally via "export REGGIE_GITLAB_CI=1" or commandline "reggie... --gitlab-ci"
        gitlab_ci_env = os.getenv('REGGIE_GITLAB_CI')
        if gitlab_ci_env:
            self.gitlab_ci = True
        else:
            self.gitlab_ci = False

    @staticmethod
    def _pump(pipe, tag, q):
        """Read lines from a pipe and push them onto a shared queue as (tag, line)."""
        for line in iter(pipe.readline, ''):
            q.put((tag, line))
        pipe.close()
        q.put((tag, None))  # sentinel: this stream is finished

    def execute_cmd(self, cmd, target_directory, name="std", string_info=None, environment=None, display_on_failure=True):
        """
        Execute an external program specified by 'cmd'. The working directory of this program is set to target_directory.

        Returns the return_code of the external program.
        cmd                                       : command given as list of strings (the command is split at every white space occurrence)
        target_directory                          : path to directory where the cmd command is to be executed
        name (optional, default="std")            : [name].std and [name].err files are created for storing the std and err output of the job
        string_info (optional, default=None)      : Print info regarding the command that is executed before execution
        environment (optional, default=None)      : run cmd command with environment variables as given by environment=os.environ (and possibly modified)
        display_on_failure (optional, default=True) : Display error information if the code has failed to run: the last 15 lines of std.out and the last 15 lines of std.err
        """
        log = logging.getLogger('logger')

        # Display string_info
        if string_info is not None:
            if self.gitlab_ci:
                print(string_info, end=' ')  # skip line break
            else:
                print(string_info)

        # Make sure the cmd is an iterable but not a string
        if isinstance(cmd, str) or not isinstance(cmd, Iterable):
            print(tools.red("cmd must be of type 'list'\ncmd=") + str(cmd) + tools.red(" and type(cmd)="), type(cmd))
            sys.exit(1)
        self.workingDir = os.path.abspath(target_directory)
        # ThreadPool creates new Threads called 'Thead-N', if only one Thread is used, it's name is 'MainThread'
        is_parallel = threading.current_thread().name != 'MainThread'

        log.debug(f"In {self.workingDir} executing {cmd}")
        start = timer()

        # Check if an environment is used and load it into the subprocess if required
        environment_arg = {"env": environment} if environment else {}

        # Replace possible wild chards (*) with the globbed entries because the subprocess.Popen takes "*" literally, except when
        # called with shell=True (which however uses the /bin/sh by default)
        cmd = replace_wild_cards_recursive(cmd, self.workingDir)
        self.process = subprocess.Popen(
            cmd,
            cwd=self.workingDir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered on our side
            **environment_arg
        )

        q = Queue()
        readers = [
            threading.Thread(target=ExternalCommand._pump, args=(self.process.stdout, 'out', q), daemon=True),
            threading.Thread(target=ExternalCommand._pump, args=(self.process.stderr, 'err', q), daemon=True),
        ]
        for t in readers:
            t.start()

        self.stdout = []
        self.stderr = []
        open_streams = len(readers)
        while open_streams:
            tag, line = q.get()
            if line is None:
                open_streams -= 1
                continue
            if tag == 'out':
                self.stdout.append(line)
                # When running in parallel, it is more useful to use the job artifacts instead of the serial log
                if not is_parallel:
                    log.debug(line.rstrip('\n'))
            else:
                self.stderr.append(line)  # readline() already keeps the '\n'
                if not is_parallel:
                    log.info(line.rstrip('\n'))

        for t in readers:
            t.join()
        self.return_code = self.process.wait()
        end = timer()
        self.walltime = end - start

        # write std.out and err.out to disk
        self.stdout_filename = os.path.join(target_directory, name + ".out")
        with open(self.stdout_filename, 'w', encoding="utf-8") as f:
            f.writelines(self.stdout)

        if self.return_code != 0:
            self.stderr_filename = os.path.join(target_directory, name + ".err")
            with open(self.stderr_filename, 'w', encoding="utf-8") as f:
                f.writelines(self.stderr)
        else:
            self.stderr_filename = None

        # Display result (Successful or Failed)
        if self.return_code != 0:
            self.result = tools.red("Failed")
        else:
            self.result = tools.blue("Successful")
        if string_info is not None and not self.gitlab_ci:
            # display result and wall time in previous line and shift the text by ncols columns to the right
            ncols = len(string_info) + 1
            print(f"\033[F\033[{ncols}G " + str(self.result) + f" [{self.walltime:.2f} sec]")
        else:
            print(self.result + f" [{self.walltime:.2f} sec]")

        # Display error information if the code has failed to run: the last 15 lines of std.out and the last 15 lines of std.err
        if display_on_failure and self.return_code != 0:
            print(tools.red("".join(self.stdout[-15:])))
            print(tools.red("".join(self.stderr[-15:])))

        return self.return_code

    def kill(self):
        self.process.kill()
