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
    root = _vectorize(root)
    root = _unroll_loops(root, global_factor=cli_unroll_factor)
    return root


def _unroll_loops(root, global_factor):
    loops = _collect_unrollable_loops(root, global_factor)
    print(f"{GREEN}[UNROLL] Collected {len(loops)} unrollable loops{RESET}")
    for loop in loops:
        unroll_info = _loop_metrics(loop, global_fac=global_factor)
        if unroll_info["ur_main_trip_count"] == 0:
            print(f"{RED}[FAIL] Loop is too short to unroll, " 
                  f"skipping it.{RESET}")
            continue
        elif unroll_info["ur_cleanup_trip_count"] != 0:
            unrolled_loop = _create_unrolled_loop(loop, unroll_info)
            cleanup_loop = _create_cleanup_loop(loop, unroll_info)
            replacement = ir.StatList(None, 
                                      [unrolled_loop, cleanup_loop], 
                                      loop.symtab)
            print(f"{GREEN}[UNROLL] Replacing loop {id(loop)} with unrolled " 
                  f"version {id(unrolled_loop)} and cleanup "
                  f"{id(cleanup_loop)}.{RESET}")
        else:
            unrolled_loop = _create_unrolled_loop(loop, unroll_info)
            replacement = unrolled_loop
            print(f"{GREEN}[UNROLL] Replacing loop {id(loop)} with unrolled " 
                  f"version {id(unrolled_loop)}.{RESET}")
        loop.parent.replace(loop, replacement)
    return root

def _collect_unrollable_loops(root, global_factor):
    """Returns a list of regular for loops that can be unrolled"""
    unrollable_loops = []

    def is_unrollable(node):
        """Adds unrollable loop to the collected loop list."""
        if isinstance(node, ir.ForStat):
            print(f"{GREEN}[UNROLL] Found for loop {id(node)}{RESET}")
            # Negative and 0 values fall back to global unroll factor
            if node.unroll_fac <= 0:
                node.unroll_fac = global_factor
            # Unroll only if induction variable is not assigned inside the 
            # loop, unroll factor != 1 and loop doesn't contain another for loop
            safe_ind_assigment = not _check_ind_assignment(node, [node.ind_sym])
            is_not_loop_nest = not _body_contains_for_stat(node)
            if safe_ind_assigment and is_not_loop_nest and node.unroll_fac != 1:
                print(f"{GREEN}[UNROLL] Found unrollable loop {id(node)} with " 
                      f"factor {node.unroll_fac}{RESET}")
                unrollable_loops.append(node)
    
    root.navigate(is_unrollable)
    return unrollable_loops

def _check_ind_assignment(loop, symbols):
    """Returns True if induction variable is assigned inside loop body"""
    assigned = False
    
    def is_regular(node):
        """Check if induction variable is assigned to insisde the loop body"""
        if isinstance(node, ir.AssignStat) and node.symbol in symbols:
            print(f"{RED}[FAIL] Found assignment to induction variable "
                  f"{node.symbol} inside body of {id(loop)}{RESET}")
            nonlocal assigned
            assigned = True

    loop.body.navigate(is_regular)
    return assigned

def _loop_metrics(loop, global_fac=None, tile_sz=None):
    """Calculate info to build unrolled and correction loops"""
    info = {}
    if global_fac is not None:
        unroll_factor = loop.unroll_fac
        info["ur_main_trip_count"] = loop.trip_count // unroll_factor
        info["ur_cleanup_trip_count"] = loop.trip_count % unroll_factor
        if info["ur_cleanup_trip_count"] != 0:
            main_trip_count = info["ur_main_trip_count"]
            step = loop.step_int
            cl_start = _offset_expr(loop.start, 
                                    main_trip_count * unroll_factor * step)
            info["ur_cleanup_start"] = cl_start
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
    """Memo dict to copy node with shallow copy for symbols and deep 
    copying the rest of the IR node"""
    memo = {}
    for sym in node.symtab:
        memo[id(sym)] = sym
    # Since loop is body.parent, this avoids copying 
    # the original ForStat over and over
    memo[id(node)] = None
    return memo

def _create_unrolled_loop(loop, info):
    """Returns main unrolled loop"""
    return ir.ForStat(None, 
                      loop.ind_sym, 
                      ir._clone_node(loop.start), 
                      info["ur_main_trip_count"],
                      loop.step_int, 
                      loop.step_int*info["ur_factor"], 
                      _clone_body(loop, info["ur_factor"]), 
                      1, 
                      loop.symtab)

def _create_cleanup_loop(loop, info):
    """Returns cleanup loop"""
    return ir.ForStat(None, 
                      loop.ind_sym, 
                      info["ur_cleanup_start"], 
                      info["ur_cleanup_trip_count"],
                      loop.step_int, 
                      loop.step_int, 
                      _clone_body(loop, 1), 
                      1, 
                      loop.symtab)

def _tile_loops(root, tile_sz):
    """Replaces loop nest of 2 with tiled version"""
    loops = _collect_tilable_loops(root, tile_size=tile_sz)
    for outer, inner in loops:
        info_outer = _loop_metrics(outer, tile_sz=tile_sz)
        info_inner = _loop_metrics(inner, tile_sz=tile_sz)

        inner_repl = _build_inner_tiled_replacement(inner, tile_sz, info_inner)
        outer_repl = _build_tiled_loop(outer, tile_sz, info_outer, inner_repl)
        # Append cleanup loop for outer, 
        # using the tiled version of the inner loop
        if info_outer["tl_cleanup"] != 0:
            inner_repl_cleanup = _build_inner_tiled_replacement(inner, 
                                                                tile_sz, 
                                                                info_inner)
            outer_cleanup = _build_tile_cleanup_loop(outer, 
                                                     tile_sz, 
                                                     info_outer, 
                                                     inner_repl_cleanup)
            outer_repl = ir.StatList(None, 
                                     [outer_repl, outer_cleanup], 
                                     outer.symtab)
        outer.parent.replace(outer, outer_repl)
    return root

def _collect_tilable_loops(root, tile_size):
    """Returns a list of the regular for loops in the program"""
    tilable_loops = []

    def is_nest(node):
        """Adds a tilable loop nest to the collected loop list"""
        if not isinstance(node, ir.ForStat):
            return
        
        outer = node
        inner = _single_for_loop(outer.body)

        if not isinstance(inner, ir.ForStat):
            return
        # Tile only two-level perfect nests
        is_innermost_loop = not _body_contains_for_stat(inner)
        if not is_innermost_loop:
            return
        
        ind_syms = _collect_ind_sym_nest(inner)
        safe_ind_assignment = not _check_ind_assignment(inner, ind_syms)
        trip_count_check_inner = inner.trip_count > tile_size 
        trip_count_check_outer = outer.trip_count > tile_size
        if (safe_ind_assignment and
            trip_count_check_inner and
            trip_count_check_outer):
                print(f"{GREEN}[TILING] Found perfect loop nest "
                        f"with outer loop {id(outer)}{RESET}")
                tilable_loops.append((outer, inner))
    
    root.navigate(is_nest)
    return tilable_loops

def _single_for_loop(body):
    """Returns the for statement in the body of the loop 
    only if the body contains only one for statement"""
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
            print(f"{RED}[FAIL] Innermost loop contains a for loop.{RESET}")
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
                      body = _build_point_loop(loop, 
                                               tile_sz, 
                                               tile_sym, 
                                               inner_body),
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
    offset = info["tl_full_tiles"] * tile_sz * loop.step_int
    return ir.ForStat(None,
                      ind_sym=loop.ind_sym,
                      start=_offset_expr(loop.start, offset),
                      trip_count=info["tl_cleanup"],
                      step=loop.step_int,
                      chunk_step_int=loop.step_int,
                      body=body,
                      unroll=loop.unroll_fac,
                      symtab=loop.symtab)

def _build_inner_tiled_replacement(inner, tile_sz, info_inner):
    """Returns tiled version of inner loop, 
    with correction appended if needed"""
    inner_body = _clone_body(inner, 1)
    inner_repl = _build_tiled_loop(inner, tile_sz, info_inner, inner_body)

    if info_inner["tl_cleanup"] != 0:
        inner_cleanup = _build_tile_cleanup_loop(inner, 
                                                 tile_sz, 
                                                 info_inner, 
                                                 _clone_body(inner, 1))
        inner_repl = ir.StatList(None, 
                                 [inner_repl, inner_cleanup], 
                                 inner.symtab)

    return inner_repl

def _create_tile_sym(loop):
    """Creates new symbol for the tile variable"""
    global ctr
    # Avoid having symbols with the same name by using ctr that gets increased every time this function runs
    new = ir.Symbol(name=loop.ind_sym.name+"_tile"+str(ctr), 
                    stype=loop.ind_sym.stype)
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

def _vectorize(root):
    """Replaces loops with a[i] = b[i] + <c[i]> and step 1 with 
    vectorized versions"""
    vec_loops = _collect_vectorizable_loops(root)
    for loop in vec_loops:
        trip_count = loop.trip_count // 2
        stat = loop.body.children[0]
        step = 2
        body = ir.VectorArrayOp(None, 
                                stat.symbol, 
                                stat.expr.children[1].symbol, 
                                stat.expr.children[2].symbol,
                                loop.ind_sym, 
                                stat.expr.children[0],
                                loop.symtab
                                )
        repl = ir.ForStat(parent=None,
                          ind_sym=loop.ind_sym,
                          start=ir._clone_node(loop.start),
                          trip_count=trip_count,
                          step=step,
                          chunk_step_int=step,
                          body=body,
                          unroll=loop.unroll_fac,
                          symtab=loop.symtab
                          )
        if loop.trip_count % 2 != 0:
            offs = (loop.trip_count // 2) * 2 * loop.step_int
            cleanup_start = _offset_expr(loop.start, offs)
            cleanup = ir.ForStat(
                parent=None,
                ind_sym=loop.ind_sym,
                start=cleanup_start,
                trip_count=1,
                step=loop.step_int,
                chunk_step_int=loop.step_int,
                body=_clone_body(loop, 1),
                unroll=loop.unroll_fac,
                symtab=loop.symtab
            )
            repl = ir.StatList(None, [repl, cleanup], loop.symtab)
        loop.parent.replace(loop, repl)
    return root

def _collect_vectorizable_loops(root):
    """Add vectorizable loops to array"""
    loops = []
    def vect(node):
        if isinstance(node, ir.ForStat):
            # Ascending loop with step 1 and trip_count >= 2
            if node.step_int == 1 and node.trip_count >= 2:
                if _body_is_arr_op(node):
                    print(f"{GREEN}[VEC] Found vectorizable loop {id(node)}"
                          f"{RESET}")
                    loops.append(node)
    
    root.navigate(vect)
    return loops

def _body_is_arr_op(loop):
    """Returns true if body has only one statement 
    and it is a operation between short array"""
    body = loop.body
    if isinstance(body, ir.StatList):
        if (len(body.children) == 1 and 
            isinstance(body.children[0], ir.AssignStat)):
            stat = body.children[0]
            if _check_array_op(stat, loop.ind_sym):
                print(f"{GREEN}[VEC] Loop {id(loop)} contains only one "
                      f"statement with only one array operation inside{RESET}")
                return True
    return False

def _check_array_op(assign_stat, ind_sym):
    """Returns True if the assign stat is between array of short ints with
    plus or minus as operation"""
    dest = assign_stat.symbol
    offset_lhs = assign_stat.offset
    expr = assign_stat.expr

    return (_check_vector_expr(expr, ind_sym) and 
            _check_vector_dest(dest, offset_lhs, ind_sym))
    
def _check_vector_dest(dest, offs, loop_sym):
    """Returns True if statement destination is an array element with index
    loop symbol"""
    if not isinstance(dest.stype, ir.ArrayType):
        print(f"{RED}[VEC] Destination is not array{RESET}")
        return False
    if not _is_short_induction_offset(offs, loop_sym):
        print(f"{RED}[VEC] Destination induction symbol is "
              f"not the same as loop{RESET}")
        return False
    if dest.stype.basetype != ir.TYPENAMES['short']:
        print(f"{RED}[VEC] Array is not of shorts{RESET}")
        return False
    return True

def _check_vector_expr(expr, loop_sym):
    """Returns True if statement expression is either a sum/difference between
    array elements with index loop symbol"""
    if isinstance(expr, ir.BinExpr):
        op = expr.children[0]
        term1 = expr.children[1]
        term2 = expr.children[2]
        if not isinstance(term1, ir.ArrayElement):        
            print(f"{RED}[VEC] Term 1 of expression is not array{RESET}")
            return False
        if not isinstance(term2, ir.ArrayElement):
            print(f"{RED}[VEC] Term 2 of expression is not array{RESET}")
            return False
        if not _is_short_induction_offset(term1.offset, loop_sym):
            print(f"{RED}[VEC] Term 1 induction symbol is "
                  f"not the same as loop{RESET}")
            return False
        if not _is_short_induction_offset(term2.offset, loop_sym):
            print(f"{RED}[VEC] Term 2 induction symbol is "
                  f"not the same as loop{RESET}")
            return False
        if term1.symbol.stype.basetype != ir.TYPENAMES['short']:
            print(f"{RED}[VEC] Array is not of shorts{RESET}")
            return False
        if term2.symbol.stype.basetype != ir.TYPENAMES['short']:
            print(f"{RED}[VEC] Array is not of shorts{RESET}")
            return False 
        if op not in ('plus', 'minus'):
            print(f"{RED}[VEC] Operation not supported{RESET}")
            return False
    else:
        return False
    return True

def _is_short_induction_offset(offset_expr, symbol):
    """Returns true if offset expression is loop symbol * 2"""
    if not isinstance(offset_expr, ir.BinExpr):
        return False
    op = offset_expr.children[0]
    var = offset_expr.children[1]
    step = offset_expr.children[2]
    if op != 'times':
        return False
    if not isinstance(var, ir.Var):
        return False
    if var.symbol != symbol:
        return False
    if not isinstance(step, ir.Const):
        return False
    if step.value != 2:
        return False
    return True