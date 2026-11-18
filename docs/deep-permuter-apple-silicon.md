# Deep permuter throughput on Apple Silicon

Research date: 2026-11-17. Scope: manual Deep permuter with the project's existing compiler and exact-byte judge. The research below describes the baseline and the prototypes. The checkout now includes the parallel ROM search and the optimized iSH build recipe.

## Findings

The baseline tested candidates serially. A scratch fixed-corpus experiment improved judge throughput from 6.45 to 18.98 candidates/second using six workers plus an optimized iSH build. The baseline iSH binary matched an unoptimized Meson debug build. The current recipe selects Meson's release build. [1][2][3][4][5][18]

## Local measurements

### Gran Turismo 4 with parallel judging and release iSH

The final implementation was tested on isolated copies of the GT4 PS2 project using its EE-GCC 2.96 configuration. Each version ran the same two seeds with a 64-variant cap and a one-minute deadline. The two historical near misses used saved pre-match C from the project's attempt history. The exact-match result uses the existing linked-byte judge.

| Function | Seed | Serial debug iSH | Parallel release iSH | Result |
| --- | ---: | ---: | ---: | --- |
| `func_01010C38` | 1847 | 5.49 s | 2.19 s | Byte-identical in both |
| `func_01010C38` | 9193 | 13.18 s | 2.55 s | Byte-identical in both |
| `func_01000FD8` | 1847 | 16.84 s | 4.27 s | Byte-identical in both |
| `func_01000FD8` | 9193 | 34.42 s | 4.42 s | Score stayed at 40 through 64 variants |

Three currently open functions ended with the same scores in both builds. `func_0100BC98` finished 64 variants in 4.33–4.42 s versus 27.88–31.41 s originally. `func_01012388` used the serial fallback when the generator could not parse `__complex__ int`. Its 64-variant runs took 15.61–17.43 s versus 32.63–34.79 s. `func_010064B8` finished 64 variants in 11.40–12.62 s. The original needed 59.67 s for 64 variants on one seed and reached its 60 s limit after 58 variants on the other.

At the desktop's one-second incremental polling cadence, `func_010064B8` finished 32 variants in 5.49 s versus 14.89 s. Live status request p95 was 173 ms with six workers (six samples) and 47 ms with the original (15 samples). With three clients repeatedly requesting full history, the median of the ten per-search p95s went from 255 ms to 319 ms. These measurements cover local HTTP responsiveness during a search. They do not measure window rendering or input latency.

In a separate cancellation run on `throw_helper`, three active iSH compiler children were gone and the claim cleared by the time the job reported cancellation, 355 ms after the request. The serial baseline took 168 ms with one active compiler. The parallel pool has six workers on this Mac and all compiler jobs still share eight slots. Imported build trees use the serial path because their trials modify a shared workspace.

### Initial fixed-corpus experiments

The research coordinator first ran two 56-candidate small-header fixed sweeps per width using the existing Apple Silicon iSH/EE-GCC 2.96 judge in a scratch experiment. Every width returned the same score vector. The test measures judging a fixed corpus, not the shipped Deep search, search feedback quality, UI responsiveness or time to exact match. [18]

| Concurrent trials | Sweep 1, candidates/s | Sweep 2, candidates/s |
| --- | ---: | ---: |
| 1 | 6.798 | 7.695 |
| 2 | 12.058 | 12.848 |
| 4 | 20.400 | 21.983 |
| 6 | 27.403 | 27.146 |
| 8 | 33.402 | 32.771 |

A more representative follow-up used a 10,382-byte synthetic header, close in size to the real project's latest 10,427-byte header, and 28 deterministic source variants. Clean reversed-order runs compared the original debug runtime with a scratch release build of the same pinned iSH commit. The original binary's hash matched the local Meson debug build exactly (`optimization=0`); the scratch release used `-O3`. Object SHA-256 hashes and scores were unchanged across every compared configuration. These short runs establish parity for this corpus only. [18]

| Workers | Original debug, median candidates/s | Release, median candidates/s | Release improvement |
| --- | ---: | ---: | ---: |
| 1 | 6.4505 | 7.416 | 15% |
| 6 | 14.767 | 18.979 | 29% |

Six release workers delivered **2.94×** the serial original throughput on this corpus. A separate short headroom experiment temporarily raised the scratch compiler queue to 12 slots: release medians were 20.386/s at eight workers, 23.6415/s at ten, and 19.540/s at twelve. Ten was fastest of those short bursts; twelve regressed. This does not justify a ten-worker production default. Start by testing four to six workers within the current shared cap, then qualify longer sustained runs, UI/agent latency, thermal state and cancellation before considering an explicit maximum-throughput mode. [18]

The representative serial debug iSH runner cost was about 149 ms, including roughly 7.2 ms for the APFS profile clone; comparison cost was about 1.66 ms. Removing cloning alone therefore cannot account for most serial latency. The machine reported M5 Pro, model Mac17,9, 48 GiB RAM, with sysctl core classes named Super (6) and Performance (12). The real project's compiler configuration was inspected read-only and defaulted to EE-GCC 2.96; no real-project speed or matching experiment was performed. A 330 KB stress run was stopped, and an early overlapping ABBA run was discarded; there is no conclusion here for that large-header workload. The production app, its runtime and real projects were not changed. [18]

### How the baseline ran

Module-level `deep_permuter.run` submits one whole search to the shared pool. Module-level `_run` checks a fresh baseline, tries up to eight local rewrites, and then calls `Permuter._permute` repeatedly. That method gets a batch of candidates and calls `_score` on each in a serial `for` loop. `MAX_WORKERS = 4` controls simultaneous background jobs across the app; `max_searches()` allows at most two manual searches while reserving a background worker where possible. These are different from workers within one search. [1][2][3]

The app does not run upstream `permuter.py` as its search driver. Its own `permuter_gen.py` imports the upstream randomizer and emits candidates; the app's normal compiler/linker/judge scores them and records improvements. Upstream's `-j N` is implemented using `multiprocessing.Process`, despite its help calling them threads. Consequently, upstream demonstrates a viable concurrency pattern, but adding `-j` to a launcher does not configure the app's Deep search. Upstream's `--show-timings` and `--speed` likewise belong to its standalone driver. [2][6][7][17]

The existing generator stays alive for a turn and caches the header parse for a source. Its own comments report roughly one second to parse a 300 KB header, versus 0.1 seconds to write 64 variants; those are historical observations, not current M5 measurements. Candidate generation and judging are currently sequential batches rather than a pipeline. [2][6]

### iSH and compiler alternatives

For a Linux compiler on macOS, `linuxvm.run` prefers the configured iSH runtime. `ish_runner.run` clones a prepared profile with `cp -cR`, writes the candidate workspace, starts `sandbox-exec` plus a new emulator, and reads outputs for every compiler invocation. Each trial therefore has setup and process-start costs in addition to emulation. A bounded per-search worker with private scratch state could amortize some setup, but preserving cancellation, input isolation, output cleanup, memory/time limits and sandboxing is essential. Reusing a filesystem alone does not preserve iSH's in-process translation state between separate emulator launches. [8][9]

iSH is user-mode x86 emulation and syscall translation. Its interpreter uses threaded gadget dispatch, rather than a JIT producing host machine code. Its README's 3–5× figure compares its interpreter design with a simpler switch-dispatch emulator; it is not a forecast for this application or M5 Pro. [10]

The baseline preparation script omitted `--buildtype`. The pinned upstream project defaults do not override build type. Meson documents default `buildtype=debug`, `optimization=0`; `debugoptimized` uses optimization level 2. Matching binary hashes and build metadata confirmed the original runtime's unoptimized build. The scratch `--buildtype=release` build measured a 15–29% gain on the representative corpus above. [4][5][11][18]

The current Colima route starts a fresh restricted container per compiler job, copies source/header inputs, and returns selected objects. It preserves the original compiler binary, but container startup and transport may outweigh a faster execution backend on small functions. Its documented default VM allocation is two CPUs and 3 GiB; that can limit a future parallel search. Benchmark a dedicated scratch VM before choosing it over iSH. [8][12]

Sony EE-GCC 2.9/2.96/3.2 and SN Systems EE-GCC 2.95.2/2.95.3 are separate pinned compiler packs. The latter use wibo on macOS; switching to them is a compiler change, not an execution-only optimization, and cannot be assumed to preserve Sony code generation. Likewise, a modern native PS2 cross-GCC is not a drop-in matching compiler. Native builds of the original Sony compiler packs remain described as planned in this repository. This research did not establish an available native macOS replacement with demonstrated parity for those Sony packs. [12][13]

Static recompilation can remove emulator dispatch for a particular binary: the separate IDO project converts selected IRIX toolchain programs to modern Linux/macOS programs and supports native macOS builds. decompdecomp already pins these IDO recompilations for IDO games. That is evidence for the technique, not proof that an equivalent Sony EE-GCC recompiler exists or that using IDO would match an EE-GCC game. A proposed Sony port/recompilation needs its own output-parity qualification. [13][14]

### Apple scheduling

All shared pool threads currently set `QOS_CLASS_UTILITY` on macOS. Apple describes Utility as balancing performance and energy for work lasting seconds to minutes, and User Initiated as emphasizing performance for explicitly requested work. A separate manual-search performance mode could benchmark User Initiated while leaving automatic work at Utility. QoS influences scheduling and core placement; it does not promise particular cores or a fixed throughput improvement. Do not infer compiler subprocess QoS from the parent thread alone: measure the actual compiler processes and core residency. [3][15]

Apple recommends dynamically distributing many work items across heterogeneous cores instead of statically allocating equal work to workers. For this search that suggests a bounded queue of candidates consumed as workers become available. Benchmark worker counts such as 1, 2 and 4 before trying more; the pool's existing comments report Python judge contention and worsening UI latency at higher concurrency on a different machine. Those historical figures do not establish the best M5 Pro count. The inspected compiler working tree also has an eight-slot shared compiler queue; additional search workers must respect that global budget rather than multiplying independent caps. [3][15][16]

### Search quality and correctness

Upstream says random permutation is most useful late in matching, when register allocation differences dominate; functional differences and reorderings often need manual analysis. More trials per second cannot rescue a wrong compiler/flags, wrong data types, missing side effects, incorrect control flow, or an unsupported parser construct. Choose realistic near misses and report time to improvement and exact match, not just raw trial count. Upstream also warns that randomization can produce nonsensical source changes that improve register allocation by accident. [7]

Preserve the existing judge as the acceptance authority. ROM mode explicitly records a match from linked byte equality and prevents a scalar zero from masquerading as a match. Upstream's disassembly score is useful for guiding its own search but is not a replacement for this project's linked-byte check. Parallel trials should compile immutable candidate snapshots and return objects/results to a single coordinator; publication must still verify unchanged compiler/header/target scope, current claim ownership, and that a newer best result is not overwritten. Imported projects currently hold their unit for a trial turn; parallel trials in a shared source/build tree require isolated workspaces rather than concurrent writes to that tree. [1][2][7][16]

The existing Deep search already caches up to 1,024 recent ROM trials by input scope and candidate source hash. Imported projects intentionally do not reuse those results because external build dependencies can change. Any larger cache needs full compiler, flags, header, target, address/relocation, language and build-dependency identity; a cache keyed only by source is unsafe. [1]

## Proposed experiment order

1. Capture stage timing on a disposable homemade near miss: baseline, generation, profile clone/staging, compiler process, linking/diff, and progress/database work. Record warm and cold rates separately.
2. Compare current iSH with a separate optimized iSH build on the identical original compiler and candidate corpus. Require successful and failed compile parity plus object and linked-byte parity.
3. Measure 1/2/4 concurrent independent compile trials with private workspaces. First use a fixed candidate corpus to isolate throughput, then multiple deterministic search seeds to measure search quality and time to match.
4. Compare Utility with User Initiated as a bounded manual-only experiment, including GUI responsiveness, CPU placement, sustained power/thermal state and agent compile latency.
5. If setup dominates, prototype a persistent per-search runner or worker-local scratch clone. If emulator execution dominates, investigate exact original-toolchain recompilation/native hosting instead. Compare scratch Colima only if its startup overhead can be amortized.

A result should include unique compiled candidates/second, compile success rate, duplicate rate, improvement rate, time to exact match, cancellation latency, peak memory and UI latency. Keep time and variant budgets global across workers, stop in-flight work at a match, and retain compiler sandboxing. These are proposed evaluation criteria, not completed validations. [1][3][8][9][15]

## Sources

1. [Manual search implementation](../src/decompnet/app/deep_permuter.py): `Search`, `_score`, `_run`, `max_searches` (working tree read 2026-11-17).
2. [App permuter](../src/decompnet/app/permute.py): `Generator`, `Permuter._permute`, `_score`.
3. [Shared background workers](../src/decompnet/app/workers.py): pool limit, historical benchmark comment and macOS QoS.
4. [Pinned iSH preparation script](../packaging/macos/prepare-ish.sh).
5. [Meson built-in options](https://mesonbuild.com/Builtin-options.html): buildtype/optimization defaults and mapping.
6. [Candidate generator](../src/decompnet/permuter_gen.py).
7. [Pinned upstream decomp-permuter README](https://github.com/simonlindholm/decomp-permuter/blob/8556c81d80d1c1af98a858c8f4dc951357f29139/README.md): setup, FAQ, compiler-specific weights, distributed mode.
8. [Linux compiler dispatch](../src/decompnet/linuxvm.py): `_container`, `_invoke`, `run`.
9. [iSH runner](../src/decompnet/ish_runner.py): `run`.
10. [Pinned iSH README](https://github.com/ish-app/ish/blob/ce96e698f1820b9d8db47e87c501ecb89119d7ce/README.md): emulator design and logging.
11. [Pinned iSH Meson project](https://github.com/ish-app/ish/blob/ce96e698f1820b9d8db47e87c501ecb89119d7ce/meson.build).
12. [Existing macOS PS2 compiler note](linux-compiler-vm.md).
13. [Pinned toolchain manifest](../src/decompnet/toolchains.toml): EE-GCC, IDO and decomp-permuter entries.
14. [IDO static recompilation README](https://github.com/decompals/ido-static-recomp/blob/main/README.md) (retrieved 2026-11-17; moving branch).
15. [Apple: Tuning performance for Apple silicon](https://developer.apple.com/documentation/apple-silicon/tuning-your-code-s-performance-for-apple-silicon): QoS, heterogeneous core scheduling, dynamic work distribution, profiling.
16. [Compiler and judge path](../src/decompnet/compiler.py), [fork trial coordination](../src/decompnet/app/forkengine.py), [fork project builds](../src/decompnet/forks/project.py), and [repository acceptance rules](../AGENTS.md).
17. [Pinned upstream main driver](https://github.com/simonlindholm/decomp-permuter/blob/8556c81d80d1c1af98a858c8f4dc951357f29139/src/main.py): `multiprocess_worker`, `run_inner`, `-j`, `--show-timings` and `--speed`.
18. Local scratch benchmark evidence, inspected 2026-11-17: [`bench.log`](/tmp/decomp-deep-speed.RVj6ei/bench.log) (first ten small-header rows only), [`runner-abba.json`](/tmp/decomp-deep-speed.RVj6ei/runner-abba.json), and [`queue-headroom.json`](/tmp/decomp-deep-speed.RVj6ei/queue-headroom.json). These temporary files may not survive cleanup; the tables preserve the reported measurements. The latter two files explicitly record identical object SHA-256 and score results.

GitHub grep was used to locate upstream multiprocessing and iSH Meson declarations; claims were followed to the pinned upstream files rather than relying on search snippets.
