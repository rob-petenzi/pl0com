#!/usr/bin/env python3

"""The main function of the compiler, AKA the compiler driver"""

import lexer
import parser
import optimizations
from support import *
from datalayout import *
from cfg import *
from regalloc import *
from codegen import *
import argparse


def compile_program(text, unroll_factor, tile_size):
    lex = lexer.Lexer(text)
    pars = parser.Parser(lex)
    res = pars.program()
    print("\n\n++++++OPTIMIZATIONS++++++")
    res = optimizations.optimize(res, cli_unroll_factor=unroll_factor, tile_size=tile_size)
    print("++++++++++++\n\n")
    print('\n', res, '\n')
    return 1
    res.navigate(print_stat_list)

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
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", help="PL/0 input file path")
    parser.add_argument("--test", action="store_true", help="Compile test program (./samples/prog1.pl0)")
    parser.add_argument("-o", "--output", help="Output assembly file path")
    parser.add_argument("-u", "--unroll", type=int, default=1, help="Global unroll factor, can be specified per loop via @pragma unroll <factor>. Factor 0 means use global unroll factor, factor 1 means no unrolling.")
    parser.add_argument("-t", "--tile", type=int, default=32, help="Tile size")
    args = parser.parse_args()

    if args.test:
        with open("./samples/prog1.pl0", "r") as test_prog:
            input_file = test_prog.read()
    elif args.input:
        with open(args.input, "r") as f_in:
            input_file = f_in.read()
    else:
        parser.error("Input file is required, unless --test is used.")
    code = compile_program(input_file, args.unroll, args.tile)

    with open(args.output or "./out.s", "w") as f_out:
        f_out.write(code)

if __name__ == '__main__':
    driver_main()
