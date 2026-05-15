#!/usr/bin/env python3

"""The main function of the compiler, AKA the compiler driver"""

import lexer
import parser
from support import *
from datalayout import *
from cfg import *
from regalloc import *
from codegen import *
from optimizations import *


def compile_program(text, unroll_factor=None):
    lex = lexer.Lexer(text)
    pars = parser.Parser(lex)
    res = pars.program()
    print('\n', res, '\n')

    res.navigate(print_stat_list)
    res = optimize(res, unroll_factor)

    node_list = get_node_list(res)
    for n in node_list:
        print(type(n), id(n), '->', type(n.parent), id(n.parent))
    print('\nTotal nodes in IR:', len(node_list), '\n')
    res.navigate(lowering)
    node_list = get_node_list(res)
    print('\n', res, '\n')
    for n in node_list:
        print(type(n), id(n))
        try:
            n.flatten()
        except Exception:
            pass
    # exit()
    # res.navigate(flattening)
    print('\n', res, '\n')

    print_dotty(res, "log.dot")

    print("\n\nDATALAYOUT\n\n")
    perform_data_layout(res)
    print('\n', res, '\n')

    cfg = CFG(res)
    cfg.liveness()
    cfg.print_liveness()
    cfg.print_cfg_to_dot("cfg.dot")

    print("\n\nREGALLOC\n\n")
    ra = LinearScanRegisterAllocator(cfg, 11)
    reg_alloc = ra()
    print(reg_alloc)

    print("\n\nCODEGEN\n\n")
    code = generate_code(res, reg_alloc)
    print(code)

    return code


def driver_main():
    """Parses parameters and runs the compiler.
    If no parameter is provided, a default test program is used.
    If one is provided it is interpreted as the input file."""
    from lexer import __test_program
    import argparse

    test_program=__test_program
    parser = argparse.ArgumentParser(description="PL/0 Compiler")
    parser.add_argument("input_file", nargs='?', help="Path to the input PL/0 file")
    parser.add_argument("output_file", nargs='?', help="Path to write the compiled code")
    parser.add_argument("-u", "--unroll", type=int, default=None, 
                        help="Global loop unroll factor")
    
    global args
    args = parser.parse_args()
    test_program = __test_program
    
    if args.input_file:
        with open(args.input_file, 'r') as inf:
            test_program = inf.read()
    code = compile_program(test_program, args.unroll)

    if args.output_file:
        with open(args.output_file, 'w') as outf:
            outf.write(code)


if __name__ == '__main__':
    driver_main()
