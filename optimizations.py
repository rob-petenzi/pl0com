#!/usr/bin/env python3

"""Optimization passes for the PL/0 IR.

This module is intentionally standalone: the compiler driver can import it and
call ``optimize(root, unroll_factor)`` after parsing and before lowering.
"""

import copy

import ir


# Fallback loop unroll factor.  Precedence is:
#   1. per-loop pragma stored in ForStat.unroll_fac
#   2. command-line value passed to optimize(..., unroll_factor=...)
#   3. this global value
GLOBAL_UNROLL_FACTOR = 1


class OptimizationReport:
    def __init__(self):
        self.unrolled_loops = 0
        self.skipped_loops = []

    def skipped(self, loop, reason):
        self.skipped_loops.append((loop, reason))


LAST_REPORT = OptimizationReport()


def optimize(root, unroll_factor=None):
    """Run all optimization passes and return the possibly replaced root node."""
    return loop_unrolling(root, unroll_factor)

def loop_unrolling(root, unroll_factor=None):
    """Unroll regular for loops in ``root``.

    The pass runs before lowering.  A regular loop is one whose body does not
    assign to the loop induction symbol nor to any symbol used as the loop step.
    """
    global LAST_REPORT

    report = OptimizationReport()
    for_loop_nodes = [node for node in _navigate_nodes(root) if isinstance(node, ir.ForStat)]

    for loop in for_loop_nodes:
        factor = get_unroll_factor(loop, unroll_factor)

        if factor is None or factor <= 1:
            report.skipped(loop, "unroll factor is not greater than one")
            continue

        if not is_regular_for_loop(loop):
            report.skipped(loop, "loop body assigns to induction or step variables")
            continue

        replacement = unroll_for_loop(loop, factor)
        if replacement is None:
            report.skipped(loop, "could not compute static trip count")
            continue

        parent = loop.parent
        if parent is None:
            root = replacement
            replacement.parent = None
            report.unrolled_loops += 1
        elif parent.replace(loop, replacement):
            report.unrolled_loops += 1
        else:
            report.skipped(loop, "could not replace loop in parent")

    LAST_REPORT = report
    return root


def get_unroll_factor(loop, cli_unroll=None, global_unroll_factor=None):
    """Choose an unroll factor with pragma > command line > global precedence."""
    if global_unroll_factor is None:
        global_unroll_factor = GLOBAL_UNROLL_FACTOR

    for candidate in (getattr(loop, "unroll_fac", None), cli_unroll, global_unroll_factor):
        factor = _as_int(candidate)
        if factor is not None:
            return factor
    return None


def is_regular_for_loop(loop):
    """Return True when the loop body does not modify forbidden symbols."""
    forbidden = {loop_induction_symbol(loop)}
    forbidden.update(loop_step_symbols(loop))
    forbidden.discard(None)
    return forbidden.isdisjoint(assigned_symbols(loop.body))


def loop_induction_symbol(loop):
    """Extract the induction symbol from a ForStat."""
    try:
        return loop.ind_var.symbol
    except AttributeError:
        return None


def loop_step_symbols(loop):
    """Return symbols used by the step expression, excluding the induction var."""
    induction = loop_induction_symbol(loop)
    try:
        uses = set(loop.step.expr.collect_uses())
    except AttributeError:
        uses = set()
    uses.discard(induction)
    return uses


def assigned_symbols(node):
    """Collect symbols assigned anywhere in ``node``."""
    assigned = set()
    for current in _navigate_nodes(node):
        try:
            assigned.update(current.collect_kills())
        except AttributeError:
            pass
    assigned.discard(None)
    return assigned


def unroll_for_loop(loop, factor):
    """Build an unrolled replacement for ``loop``.

    The replacement is:

        loop.init
        while main_cond:
            loop.body
            loop.step
            clone(loop.body)
            clone(loop.step)
            ...
        cleanup body/step copies
    """
    static = _static_loop_info(loop, factor)
    if static is not None:
        return _unroll_static_for_loop(loop, factor, static)
    return None


def _unroll_static_for_loop(loop, factor, static):
    main_iterations = static["trip_count"] // factor
    remainder = static["trip_count"] % factor

    statements = [loop.init]

    if main_iterations > 0:
        main_cond = _make_induction_condition(loop, static["cond_op"], static["main_limit"])
        main_body = ir.StatList(
            children=_build_body_step_sequence(loop, factor, use_original=True),
            symtab=loop.symtab,
        )
        statements.append(ir.WhileStat(cond=main_cond, body=main_body, symtab=loop.symtab))
        cleanup = _build_body_step_sequence(loop, remainder)
    else:
        cleanup = _build_body_step_sequence(loop, remainder, use_original=True)

    statements.extend(cleanup)
    return ir.StatList(children=statements, symtab=loop.symtab)


def clone_statement(node):
    """Deep-copy an IR statement while keeping Symbol objects shared."""
    return _deepcopy_with_shallow_symbols(node)


def _build_body_step_sequence(loop, repetitions, use_original=False):
    statements = []
    for index in range(repetitions):
        use_current = use_original and index == 0
        statements.append(loop.body if use_current else clone_statement(loop.body))
        statements.append(loop.step if use_current else clone_statement(loop.step))
    return statements


def _static_loop_info(loop, factor):
    induction = loop_induction_symbol(loop)
    start = _const_value(getattr(loop.init, "expr", None))
    cond_op, stop = _condition_parts(loop.cond, induction)
    step_delta = _step_delta(loop.step, induction)

    if start is None or cond_op is None or stop is None or step_delta is None:
        return None

    if cond_op == "lss":
        if step_delta <= 0:
            return None
        trip_count = _ceil_div(max(0, stop - start), step_delta)
    elif cond_op == "gtr":
        if step_delta >= 0:
            return None
        trip_count = _ceil_div(max(0, start - stop), -step_delta)
    else:
        return None

    main_limit = start + ((trip_count // factor) * factor * step_delta)
    return {
        "cond_op": cond_op,
        "main_limit": main_limit,
        "trip_count": trip_count,
    }


def _condition_parts(cond, induction):
    try:
        op, lhs, rhs = cond.children
    except (AttributeError, ValueError):
        return None, None

    if op not in ("lss", "gtr"):
        return None, None

    if not _is_var_symbol(lhs, induction):
        return None, None

    stop = _const_value(rhs)
    if stop is None:
        return None, None
    return op, stop


def _step_delta(step, induction):
    try:
        op, lhs, rhs = step.expr.children
    except (AttributeError, ValueError):
        return None

    if not _is_var_symbol(lhs, induction):
        return None

    amount = _const_value(rhs)
    if amount is None:
        return None

    if op == "plus":
        return amount
    if op == "minus":
        return -amount
    return None


def _make_induction_condition(loop, op, limit):
    induction = loop_induction_symbol(loop)
    return ir.BinExpr(
        children=[
            op,
            ir.Var(var=induction, symtab=loop.symtab),
            ir.Const(value=limit, symtab=loop.symtab),
        ],
        symtab=loop.symtab,
    )


def _is_var_symbol(node, symbol):
    return isinstance(node, ir.Var) and getattr(node, "symbol", None) is symbol


def _const_value(node):
    if isinstance(node, ir.Const):
        return node.value
    return None


def _ceil_div(num, den):
    return (num + den - 1) // den


def _deepcopy_with_shallow_symbols(node):
    subtree_nodes = set(_navigate_nodes(node))
    memo = {}

    for symbol in _collect_symbols(node):
        memo[id(symbol)] = symbol

    # Parent links point out of the subtree at the clone root.  Prevent deepcopy
    # from cloning the surrounding program through that back-reference.
    parent = getattr(node, "parent", None)
    if parent not in subtree_nodes:
        memo[id(parent)] = None

    cloned = copy.deepcopy(node, memo)
    _repair_parent_links(cloned, getattr(cloned, "parent", None))
    return cloned


def _repair_parent_links(node, parent=None):
    if isinstance(node, ir.IRNode):
        node.parent = parent
        for child in _direct_ir_children(node):
            _repair_parent_links(child, node)


def _collect_symbols(obj, symbols=None, seen=None):
    if symbols is None:
        symbols = []
    if seen is None:
        seen = set()

    if obj is None:
        return symbols

    obj_id = id(obj)
    if obj_id in seen:
        return symbols
    seen.add(obj_id)

    if isinstance(obj, ir.Symbol):
        symbols.append(obj)
        return symbols

    if isinstance(obj, (str, int, bool)):
        return symbols

    if isinstance(obj, dict):
        for key, value in obj.items():
            _collect_symbols(key, symbols, seen)
            _collect_symbols(value, symbols, seen)
        return symbols

    if isinstance(obj, (list, tuple, set)):
        for value in obj:
            _collect_symbols(value, symbols, seen)
        return symbols

    if hasattr(obj, "__dict__"):
        for attr, value in vars(obj).items():
            if attr == "parent":
                continue
            _collect_symbols(value, symbols, seen)

    return symbols


def _navigate_nodes(root):
    result = []
    seen = set()

    if not isinstance(root, ir.IRNode):
        return result

    def collect(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        result.append(node)

    root.navigate(collect)
    return result


def _direct_ir_children(node):
    children = []
    seen = set()

    def add(value):
        if isinstance(value, ir.IRNode) and id(value) not in seen:
            children.append(value)
            seen.add(id(value))

    for child in getattr(node, "children", []):
        add(child)

    for attr in (
        "body",
        "cond",
        "thenpart",
        "elsepart",
        "call",
        "step",
        "expr",
        "offset",
        "init",
        "ind_var",
        "defs",
    ):
        add(getattr(node, attr, None))

    return children


def _as_int(value):
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None
