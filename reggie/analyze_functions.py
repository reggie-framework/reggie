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
import math


def get_last_L2_error(lines, name, LastLines=35):
    """
    Get L_2 eror value from a set of lines for the last timestep.

    The set of lines correspond to the output-lines of a flexi-run
    """
    for line in lines[-LastLines:]:  # read the last XX lines (default is 35)
        # search for name, e.g., "L2_Part" or "L_2"
        if name in line:
            tmp = line.split(":")[1]
    return [float(x) for x in tmp.split()]


def get_last_number_of_timesteps(lines, name, LastLines=35):
    """
    Get the number of total timesteps used for the simulation.

    The set of lines correspond to the output-lines of a flexi-run
    """
    for line in lines[-LastLines:]:  # read the last XX lines (default is 35)
        # search for name, e.g., "#Timesteps"
        if name in line:
            tmp = line.split(":")[1]
    return [float(x) for x in tmp.split()]


def get_initial_timesteps(lines, name):
    """
    Get the initial timestep used for the simulation.

    The set of lines correspond to the output-lines of a flexi-run
    """
    for line in lines:  # read all
        # search for name, e.g., "#Timesteps"
        if name in line:
            tmp = line.split(":")[1]
    return [float(x) for x in tmp.split()]


def get_last_Linf_error(lines, LastLines=35):
    """
    Get L_inf eror value from a set of lines for the last timestep

    The set of lines correspond to the output-lines of a flexi-run
    """
    for line in lines[-LastLines:]:  # read the last XX lines (default is 35)
        if "L_inf" in line:
            tmp = line.split(":")[1]
            return [float(x) for x in tmp.split()]


def get_last_number(lines):
    for line in reversed(lines):
        tmp = line.split(' ')
        for t in reversed(tmp):
            try:
                return float(t)
            except Exception:  # noqa: PERF203 `try`-`except` within a loop incurs performance overhead
                pass


def get_cpu_per_dof(lines):
    """
    Get the PID value from a set of lines

    The set of lines correspond to the output-lines of a flexi-run
    """
    for line in reversed(lines):
        if "CALCULATION TIME PER TSTEP/DOF: [" in line:
            return float(line.split("[")[1].split("sec")[0])


def calcOrder_h(h, E, invert_h=False):
    """Determine the order of convergence for a list of grid spacings h and errors E"""
    h = [1.0 / float(elem) for elem in h] if invert_h else [float(elem) for elem in h]
    E = [float(elem) for elem in E]
    if len(h) != len(E):
        return -1

    order = []
    for i in range(1, len(h)):
        dh = 1.0 / (h[i] / h[i - 1])
        # Check if any error value is exactly zero
        if abs(E[i - 1]) <= 0.0 or abs(E[i]) <= 0.0:
            order.append(0.0)
        else:
            dE = E[i] / E[i - 1]
            order.append(math.log(dE) / math.log(dh))

    return order


def calcOrder_p(p, E):
    """Determine the order of convergence for a list of polynomial degrees p and errors E"""
    p = [float(elem) for elem in p]
    E = [float(elem) for elem in E]
    if len(p) != len(E):
        return -1

    order = []
    for i in range(1, len(p)):
        dp = 1.0 / ((p[i] + 1.0) / (p[i - 1] + 1.0))
        if abs(E[i - 1]) <= 0.0:
            order.append(0.0)
        else:
            dE = E[i] / E[i - 1]
            order.append(math.log(dE) / math.log(dp))

    return order
