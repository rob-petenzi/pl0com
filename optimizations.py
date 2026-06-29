import ir
import copy

# Logger colors
RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[39m"

ctr = 0

def optimize(root, cli_unroll_factor=1, tile_size=32):
    """Apply optimizations"""
    root = _tile_loops(root, tile_size)
    root = _unroll_loops(root, global_factor=cli_unroll_factor)
    return root


def _unroll_loops(root, global_factor):
    loops = _collect_unrollable_loops(root, global_factor)
    print(f"{GREEN}[UNROLL] Collected {len(loops)} unrollable loops{RESET}")
    for loop in loops:
        unroll_info = _loop_metrics(loop, global_fac=global_factor)
        if unroll_info["ur_main_trip_count"] == 0:
            print(f"{RED}[UNROLL] Loop is too short to unroll, skipping it.{RESET}")
            continue
        elif unroll_info["ur_cleanup_trip_count"] != 0:
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
            # Unroll factor = 1 means do not unroll and don't unroll loop nests to avoid repeating for statements
            if _check_ind_assignment(node, [node.ind_sym]) and node.unroll_fac != 1 and not _body_contains_for_stat(node):
                print(f"{GREEN}[UNROLL] Found unrollable loop {id(node)} with factor {node.unroll_fac if node.unroll_fac != 0 else global_factor}{RESET}")
                unrollable_loops.append(node)
    
    root.navigate(is_for)
    return unrollable_loops

def _check_ind_assignment(loop, symbols):
    """Check if induction variable is assigned in the loop body."""
    regular = True
    
    def is_regular(node):
        if isinstance(node, ir.AssignStat) and node.symbol in symbols:
            print(f"{RED}[UNROLL] Found assignment to induction variable {node.symbol} inside body of {id(loop)}{RESET}")
            nonlocal regular 
            regular = False

    loop.body.navigate(is_regular)
    return regular

def _loop_metrics(loop, global_fac=None, tile_sz=None):
    """Calculate info to build unrolled and correction loops"""
    info = {}
    if global_fac is not None:
        # Negative or 0 value fall back to global unroll factor
        if loop.unroll_fac <= 0:
            unroll_factor = global_fac
        else:
            unroll_factor = loop.unroll_fac

        info["ur_main_trip_count"] = loop.trip_count // unroll_factor
        info["ur_cleanup_trip_count"] = loop.trip_count % unroll_factor
        if info["ur_cleanup_trip_count"] != 0:
            info["ur_cleanup_start"] = _offset_expr(loop.start, info["ur_main_trip_count"] * unroll_factor * loop.step_int)
        info["ur_factor"] = unroll_factor
    if tile_sz is not None:
        info["tl_full_tiles"] = loop.trip_count // tile_sz
        info["tl_cleanup"] = loop.trip_count % tile_sz
    return info

def _offset_expr(expr, offset):
    """Generate expression that adds offset to the base expression"""
    base = ir._clone_node(expr)
    if offset == 0:
        return base
    else:
        return ir.BinExpr(
            children=[
                "plus",
                base,
                ir.Const(value = offset, symtab=base.symtab)
            ],
            symtab=base.symtab
        )
    
def _clone_body(loop, n):
    """Returns statlist with deepcopied (body + step)*(n-1) + body"""
    new_body = []
    for _ in range(0, n - 1):    
        memo = _init_memo(loop)
        new_body.append(copy.deepcopy(loop.body, memo=memo))
        new_body.append(copy.deepcopy(loop.step, memo=memo))
    # the last step statement is added by the lowering method
    memo = _init_memo(loop)
    new_body.append(copy.deepcopy(loop.body, memo=memo))
    return ir.StatList(None, new_body, symtab=loop.symtab)

def _init_memo(node):
    """Memo dict to copy node with shallow copy for symbols and deep copying the rest of the IR node"""
    memo = {}
    for sym in node.symtab:
        memo[id(sym)] = sym
    # Since loop is body.parent, this avoids copying the original ForStat over and over
    memo[id(node)] = None
    return memo

def _create_unrolled_loop(loop, info):
    """Returns unrolled loop"""
    return ir.ForStat(None, loop.ind_sym, 
                      ir._clone_node(loop.start), info["ur_main_trip_count"],
                      loop.step_int, loop.step_int*info["ur_factor"], _clone_body(loop, info["ur_factor"]), 1, loop.symtab)

def _create_cleanup_loop(loop, info):
    """Returns cleanup loop"""
    return ir.ForStat(None, loop.ind_sym, 
                      info["ur_cleanup_start"], info["ur_cleanup_trip_count"],
                      loop.step_int, loop.step_int, _clone_body(loop, 1), 1, loop.symtab)

def _tile_loops(root, tile_sz):
    """Replaces loop nest of 2 with tiled version"""
    loops = _collect_tilable_loops(root, tile_size=tile_sz)
    for outer, inner in loops:
        info_outer = _loop_metrics(outer, tile_sz=tile_sz)
        info_inner = _loop_metrics(inner, tile_sz=tile_sz)
        inner_repl = _build_inner_tiled_replacement(inner, tile_sz, info_inner)
        outer_repl = _build_tiled_loop(outer, tile_sz, info_outer, inner_repl)
        # Append cleanup loop for outer, using the tiled version of the inner loop
        if info_outer["tl_cleanup"] != 0:
            inner_repl_cleanup = _build_inner_tiled_replacement(inner, tile_sz, info_inner)
            outer_cleanup = _build_tile_cleanup_loop(outer, tile_sz, info_outer, inner_repl_cleanup)
            outer_repl = ir.StatList(None, [outer_repl, outer_cleanup], outer.symtab)
        outer.parent.replace(outer, outer_repl)
    return root

def _collect_tilable_loops(root, tile_size):
    """Returns a list of the regular for loops in the program"""
    tilable_loops = []

    def is_nest(node):
        # Loop is tilable if is innermost loop of a perfect nest
        if isinstance(node, ir.ForStat):
            outer = node
            inner = _single_for_loop(outer.body)
            if isinstance(inner, ir.ForStat) and not _body_contains_for_stat(inner):
                if _check_ind_assignment(inner, _collect_ind_sym_nest(inner)) and inner.trip_count > tile_size and outer.trip_count > tile_size:
                    print(f"{GREEN}[TILING] Found perfect loop nest with outer loop {id(outer)}{RESET}")
                    tilable_loops.append((outer, inner))
    
    root.navigate(is_nest)
    return tilable_loops

def _single_for_loop(body):
    """Returns the for statement in the body of the loop only if the body contains only one for statement"""
    if isinstance(body, ir.ForStat):
        return body
    elif isinstance(body, ir.StatList):
        if len(body.children) == 1 and isinstance(body.children[0], ir.ForStat):
            return body.children[0]
    else:
        return None
    
def _body_contains_for_stat(loop):
    """Returns true if body contains for loop"""
    found = False

    def has_for(node):
        nonlocal found
        if isinstance(node, ir.ForStat):
            print(f"{RED}[TILING] Innermost loop contains a for loop.{RESET}")
            found = True
    
    loop.body.navigate(has_for)
    return found

def _collect_ind_sym_nest(loop):
    """Collect induction variables of nest"""
    forbidden = []
    curr = loop
    while curr is not None:
        if isinstance(curr, ir.ForStat):
            forbidden.append(curr.ind_sym)
        curr = curr.parent
    return forbidden

def _build_tiled_loop(loop, tile_sz, info, inner_body):
    """Builds the external tiled loop (the one that has stride of tile_sz)"""
    tile_sym = _create_tile_sym(loop)
    step = loop.step_int * tile_sz
    return ir.ForStat(None,
                      ind_sym=tile_sym, 
                      start=ir._clone_node(loop.start), 
                      trip_count=info["tl_full_tiles"], 
                      step=step, 
                      chunk_step_int=step,
                      body = _build_point_loop(loop, tile_sz, tile_sym, inner_body),
                      unroll=1,
                      symtab=loop.symtab)

def _build_point_loop(loop, tile_sz, tile_sym, body):
    """Build inner loop (the one that iterates over the tile variable)"""
    return ir.ForStat(None, 
                      ind_sym=loop.ind_sym, 
                      start=ir.Var(var=tile_sym, symtab=loop.symtab), 
                      trip_count=tile_sz, 
                      step=loop.step_int, 
                      chunk_step_int=loop.step_int,
                      body=body,
                      unroll=loop.unroll_fac,
                      symtab=loop.symtab)

def _build_tile_cleanup_loop(loop, tile_sz, info, body):
    """Build cleanup loop"""
    return ir.ForStat(None,
                      ind_sym=loop.ind_sym,
                      start=_offset_expr(loop.start, info["tl_full_tiles"] * tile_sz * loop.step_int),
                      trip_count=info["tl_cleanup"],
                      step=loop.step_int,
                      chunk_step_int=loop.step_int,
                      body=body,
                      unroll=loop.unroll_fac,
                      symtab=loop.symtab)

def _build_inner_tiled_replacement(inner, tile_sz, info_inner):
    """Returns tiled version of inner loop, with correction appended if needed"""
    inner_body = _clone_body(inner, 1)
    inner_repl = _build_tiled_loop(inner, tile_sz, info_inner, inner_body)

    if info_inner["tl_cleanup"] != 0:
        inner_cleanup = _build_tile_cleanup_loop(inner, tile_sz, info_inner, _clone_body(inner, 1))
        inner_repl = ir.StatList(None, [inner_repl, inner_cleanup], inner.symtab)

    return inner_repl

def _create_tile_sym(loop):
    """Creates new symbol for the tile variable"""
    global ctr
    # Avoid having symbols with the same name by using ctr that gets increased every time this function runs
    new = ir.Symbol(name=loop.ind_sym.name+"_tile"+str(ctr), stype=loop.ind_sym.stype)
    loop.symtab.append(new)
    # Add it to block's symtab so that data layout uses
    block = _enclosing_block(loop)
    block.symtab.append(new)
    ctr += 1
    return new

def _enclosing_block(node):
    """Returns enclosing block of a node"""
    curr = node
    while curr is not None and not isinstance(curr, ir.Block):
        curr = curr.parent
    return curr