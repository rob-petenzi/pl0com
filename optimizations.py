import ir
import copy

# Logger colors
RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[39m"


def optimize(root, cli_unroll_factor=1, tile_size=32):
    """Apply optimizations"""
    root = _unroll_loops(root, global_factor=cli_unroll_factor)
    return root


def _unroll_loops(root, global_factor):
    loops = _collect_unrollable_loops(root, global_factor)
    print(f"{GREEN}[UNROLL] Collected {len(loops)} unrollable loops{RESET}")
    for loop in loops:
        unroll_info = _loop_metrics(loop, global_factor)
        if unroll_info["unrolled_loop_iter"] == 0:
            print(f"{RED}[UNROLL] Loop is too short to unroll, skipping it.{RESET}")
            continue
        elif unroll_info["cleanup_loop_iter"] != 0:
            unrolled_loop = _create_unrolled_loop(loop, unroll_info)
            cleanup_loop = _create_cleanup_loop(loop, unroll_info)
            replacement = ir.StatList(None, [unrolled_loop, cleanup_loop], loop.symtab)
            print(f"{GREEN}[UNROLL] Replacing loop {id(loop)} with unrolled version {id(unrolled_loop)} and cleanup {id(cleanup_loop)}.{RESET}")
        else:
            unrolled_loop = _create_unrolled_loop(loop, unroll_info)
            replacement = unrolled_loop
            print(f"{GREEN}[UNROLL] Replacing loop {id(loop)} with unrolled version {id(unrolled_loop)}.{RESET}")
        loop.parent.replace(loop, replacement)
    return root

def _collect_unrollable_loops(root, global_factor):
    """Returns a list of the regular for loops in the program"""
    unrollable_loops = []

    def is_for(node):
        # Loop is unrollable if it is a for loop and no assignment to induction variable happen inside the body
        if isinstance(node, ir.ForStat):
            print(f"{GREEN}[UNROLL] Found for loop {id(node)}{RESET}")
            # Unroll factor = 1 means do not unroll
            if _check_ind_assignment(node) and node.unroll_fac != 1:
                print(f"{GREEN}[UNROLL] Found unrollable loop {id(node)} with factor {node.unroll_fac if node.unroll_fac != 0 else global_factor}{RESET}")
                unrollable_loops.append(node)
    
    root.navigate(is_for)
    return unrollable_loops

def _check_ind_assignment(loop):
    """Check if induction variable is assigned in the loop body."""
    regular = True
    
    def is_regular(node):
        if isinstance(node, ir.AssignStat) and node.symbol == loop.ind_sym:
            print(f"{RED}[UNROLL] Found assignment to induction variable {loop.ind_sym} inside body of {id(loop)}{RESET}")
            nonlocal regular 
            regular = False

    loop.body.navigate(is_regular)
    return regular

def _is_ascending(loop):
    """Checks if loop is ascending"""
    if loop.direction == "up":
        return True
    else:
        return False
    
def _loop_metrics(loop, global_fac):
    """Calculate info to build unrolled and correction loops"""
    info = {}
    # Negative or 0 value fall back to global unroll factor
    if loop.unroll_fac <= 0:
        unroll_factor = global_fac
    else:
        unroll_factor = loop.unroll_fac

    info["unrolled_loop_start"] = loop.start_int
    info["unrolled_loop_step"] = loop.step_int
    info["cleanup_loop_stop"] = loop.stop_int
    info["cleanup_loop_step"] = loop.step_int
    info["unroll_factor"] = unroll_factor
    if _is_ascending(loop):
        trip_count = ((loop.stop_int - loop.start_int) // loop.step_int) + 1
        unroll_iterations = trip_count // unroll_factor
        # This is so the condition becomes the start of the last unrolled chunk instead of the original loop bound
        info["unrolled_loop_stop"] = loop.start_int + ((unroll_iterations - 1) * unroll_factor * loop.step_int)
        info["cleanup_loop_iter"] = trip_count % unroll_factor
        info["cleanup_loop_start"] = loop.start_int + (unroll_iterations * unroll_factor * loop.step_int)
        info["unrolled_loop_iter"] = unroll_iterations
    else:
        trip_count = ((loop.start_int - loop.stop_int) // loop.step_int) + 1
        unroll_iterations = trip_count // unroll_factor
        info["unrolled_loop_stop"] = loop.start_int - ((unroll_iterations - 1) * unroll_factor * loop.step_int)
        info["cleanup_loop_iter"] = trip_count % unroll_factor
        info["cleanup_loop_start"] = loop.start_int - (unroll_iterations * unroll_factor * loop.step_int)
        info["unrolled_loop_iter"] = unroll_iterations
    return info

def _clone_body(loop, n):
    """Returns statlist with deepcopied (body + step)*(n-1) + body"""

    new_body = []
    for _ in range(0, n - 1):    
        memo = _init_memo_body(loop)
        new_body.append(copy.deepcopy(loop.body, memo=memo))
        new_body.append(copy.deepcopy(loop.step, memo=memo))
    # the last step statement is added by the lowering method
    memo = _init_memo_body(loop)
    new_body.append(copy.deepcopy(loop.body, memo=memo))
    return ir.StatList(None, new_body, symtab=loop.symtab)

def _init_memo_body(loop):
    """Memo dict to copy loop body with shallow copy for symbols and deep copying the rest of the IR node"""
    memo = {}
    for sym in loop.symtab:
        memo[id(sym)] = sym
    # Since loop is body.parent, this avoids copying the original ForStat over and over
    memo[id(loop)] = None
    return memo

def _create_unrolled_loop(loop, info):
    """Returns unrolled loop"""
    return ir.ForStat(None, loop.ind_sym, 
                      info["unrolled_loop_start"], info["unrolled_loop_stop"],
                      info["unrolled_loop_step"], _clone_body(loop, info["unroll_factor"]), 1, loop.direction, loop.symtab)

def _create_cleanup_loop(loop, info):
    """Returns cleanup loop"""
    return ir.ForStat(None, loop.ind_sym, 
                      info["cleanup_loop_start"], info["cleanup_loop_stop"],
                      info["cleanup_loop_step"], _clone_body(loop, 1), 1, loop.direction, loop.symtab)