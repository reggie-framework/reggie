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
from externalcommand import ExternalCommand  # ty:ignore[import-not-found]
from timeit import default_timer as timer
import sys


class bcolors:
    """color and font style definitions for changing output appearance"""

    # Reset (user after applying a color to return to normal coloring)
    # fmt: off
    ENDC   ='\033[0m'

    # Regular Colors
    BLACK  ='\033[0;30m'
    RED    ='\033[0;31m'
    GREEN  ='\033[0;32m'
    YELLOW ='\033[0;33m'
    BLUE   ='\033[0;34m'
    PURPLE ='\033[0;35m'
    CYAN   ='\033[0;36m'
    WHITE  ='\033[0;37m'

    # Text Style
    BOLD      = '\033[1m'
    UNDERLINE = '\033[4m'
    # fmt: on


class Case(ExternalCommand):
    def __init__(self, command):
        self.command = command
        self.failed = False
        ExternalCommand.__init__(self)


def finalize(start, run_errors):
    """Display if gitlab_ci script check was successful or not and return the number of errors that were encountered"""
    if run_errors > 0:
        print(bcolors.RED + 132 * '=')
        print(
            "gitlab-ci processing tool  FAILED!",
        )
        return_code = 1
    else:
        print(bcolors.BLUE + 132 * '=')
        print(
            "gitlab-ci processing tool  successful!",
        )
        return_code = 0

    if start > 0:  # only calculate run time and display output when start > 0
        end = timer()
        print("in [%2.2f sec]" % (end - start))
    else:
        print()

    print(f"Number of run     errors: {run_errors:d}")

    print('=' * 132 + bcolors.ENDC)
    sys.exit(return_code)
