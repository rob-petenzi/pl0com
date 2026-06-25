# Modifications implemented for the CTO course

## Support for `for` loops
The compiler now supports for loops in the form `for i from X to Y [by Z] do`, where X, Y, Z are constant integers.

## Loop unrolling optimization
Unroll loops by a factor. The factor can be specified for each loop via `@pragma unroll F`, as a command line argument `--unroll F` or it will fall back to an hardcoded constant (1) - do not unroll. Done before lowering.
- Replicate body F times along with step statements. Add correction loop at the end to handle the case where trip count is not divisible by unroll factor. In the replication deep copy IR nodes until symbols, which need to be kept to avoid treating them as different variables.
- Need to check that no assignments to the induction variable are done inside the body.
- Unroll only loops with positive trip count.
- TODO: possibly unroll and jam

## Loop tiling optimization
Since the qemu simulation does not represent the cache of the target CPU faithfully, I will go with the assumption that most ARMv6 had between 16KB and 32KB of cache (I'll go with 32 here). I will consider `int32` matrices.
Since a matrix operation usually requires to work with 3 tiles (NxN) - one output and two inputs, it will require $3 * N * N * 4$ B. This means that $3*N*N*4 <= 16384$, which yelds $N < 36$. Tiles size are usually multiples of 32 so if the user does not specify differently (via `--tile SZ`), the default tile size will be 32.

## Vector operations
Assume that vector operations happen between `short int`s, so I can rely on `sadd16` to perform vector operations.