# Compiler concurrency on an 18-core M5 Pro

Research date: 2026-11-17.

## Implementation

The shared compiler queue allows up to twelve slots, calculated as the reported CPU count minus two with a minimum of one. This M5 Pro gets twelve slots. A manually started ROM search now uses up to ten trial workers whenever twelve compiler slots are available. One Deep search runs at a time at that width, leaving two shared slots outside the pool. ROM searches on smaller hosts share up to six trial workers as capacity allows. Imported build trees use serial trials. The compiler queue remains shared with the rest of the app, and recognized Ninja and Make builds still take up to four jobs each. The trial pools use Utility QoS and the same compiler and byte judge. [Compiler queue](../src/decompnet/compiler.py) · [Deep trial pools](../src/decompnet/app/deep_permuter.py) · [Desktop admission](../src/decompnet/app/routes/projects.py) · [Fork builds](../src/decompnet/forks/project.py)

## Six versus ten on GT4 near misses

Both modes used the same 12-slot queue, release iSH and the project's EE-GCC 2.96 configuration. The app received actual desktop start, status and cancel requests in isolated copies of the PS2 project. Test order was six, ten, ten, six across two matched seeds. The two previously matched functions started from earlier scored C recovered from attempt history. The Mac was on battery power with other apps running.

| Function | Seed | Six workers | Ten workers | Outcome in both modes |
| --- | ---: | ---: | ---: | --- |
| `func_01010C38` | 1847 | 1.60 s | 1.51 s | Byte-identical |
| `func_01010C38` | 9193 | 2.75 s | 2.25 s | Byte-identical |
| `func_01000FD8` | 1847 | 2.36 s | 2.33 s | Byte-identical |
| `func_01000FD8` | 9193 | 3.90 s | 3.20 s | Score stayed at 40 after 64 variants |
| `func_0100BC98` | 1847 | 2.87 s | 3.17 s | Score stayed at 60 after 64 variants |
| `func_0100BC98` | 9193 | 3.73 s | 3.43 s | Score stayed at 60 after 64 variants |
| `func_010064B8` | 1847 | 39.45 s | 39.21 s | Score stayed at 20 after 256 variants |
| `func_010064B8` | 9193 | 44.98 s | 36.40 s | Score stayed at 20 after 256 variants |

The 256-variant runs averaged 42.21 s versus 37.80 s, a 10% reduction in elapsed time. Across both runs per mode, the app and reaped child processes averaged 4.47 versus 5.84 cores' worth of CPU time (25% versus 32% of aggregate time across 18 cores). This uses Darwin per-process and child CPU counters converted with `mach_timebase_info`, checked against `getrusage`. Whole-system busy time varied with concurrent work. Power draw and thermal behavior under long sustained load were outside this measurement.

With one incremental `/api/live` poll per second during those runs, pooled p95 went from 230 ms (85 samples) to 437 ms (77 samples). `/health` p95 went from 10 ms to 32 ms. These are local HTTP latencies. In the four `throw_helper` cancellation runs, terminal status arrived in 231 and 393 ms with six active iSH compiler children and in 287 and 380 ms with ten. Every run had zero compiler children and no active function claim when it finished. [Raw benchmark results](/tmp/decomp-deep-high.ns2OLq/real-benchmark.json)

## Eight versus twelve shared compiler slots

Sixteen submitter threads queued the same 96 distinct scored C candidates from the `func_010064B8` attempt history. The scratch test called the ordinary app compilation path with the real header and release iSH. It varied only the shared queue capacity, in the order eight, twelve, twelve, eight. Each run reached its slot ceiling. All 96 objects compiled and the ordered SHA-256 hashes agreed across all four runs.

| Shared cap | Elapsed per run | Mean compiles/s | App and child CPU time per second |
| ---: | --- | ---: | ---: |
| 8 | 9.02 s and 8.98 s | 10.67 | 7.86 cores' worth |
| 12 | 6.97 s and 7.52 s | 13.27 | 9.95 cores' worth |

Twelve slots processed around 24% more compiles per second with work ready throughout. The CPU time averaged about 55% of the Mac's 18-core capacity by time. This is a saturated compile replay. Typical usage depends on how many agents and project jobs are ready to compile, and this batch did not benchmark concurrent editing. [Queue benchmark results](/tmp/decomp-deep-high.ns2OLq/queue-report.json)

## Motivation

**Ten simultaneous compiler trials is the strongest existing experimental lead** for an explicit maximum-throughput mode, rather than eighteen. A twelve-slot app-wide budget with a ten-trial manual pool would leave potential headroom for other compilation, subject to the queue semantics below; sustained mixed-workload validation is still required. Existing short, representative release-iSH sweeps measured 18.98 candidates/s at six workers, 20.39 at eight, 23.64 at ten, and 19.54 at twelve. Ten was about 25% faster than six; twelve was slower than ten. These were fixed-corpus scratch measurements, not sustained mixed app/agent workloads. They establish neither an optimal production limit nor a thermal explanation for the regression. [Existing measurements](deep-permuter-apple-silicon.md#initial-fixed-corpus-experiments).

## Inspected baseline limits

| Limit | Current value on this Mac | Scope |
| --- | ---: | --- |
| Compiler queue | 8 | One Python-process-wide capacity shared by admitted compiler calls and project builds |
| Deep trial pool | 6 | One pool shared across manual ROM searches, not six workers per search |
| Background pool | 4 | Coordinators, automatic permuting and drafting |
| Manual searches | 2 | Each occupies a background worker while it runs |
| Ninja/Make jobs per project build | Up to 4 | Reserves that many compiler slots together |

These are the baseline values inspected before the proposed concurrency-mode change; subsequent implementation may differ. They come from [compiler.py](../src/decompnet/compiler.py), [deep_permuter.py](../src/decompnet/app/deep_permuter.py), [workers.py](../src/decompnet/app/workers.py), and [forks/project.py](../src/decompnet/forks/project.py), read from the working tree on the research date. Raising the compiler budget alone leaves one Deep search limited to six trial workers. Raising Deep width alone leaves compilation limited by the shared queue. Raising the background pool is a separate decision: its source records historical Python/GIL contention and UI latency regressions on another machine.

`CompilerQueue.reserve` admits FIFO waiters and reserves multi-slot builds atomically. A four-job project build takes four tokens; a regular function compile takes one. A custom build without a recognized job-count interface reserves the entire budget, but its unknown internal fan-out is not mechanically limited. Compilation acquires its reservation before preparing its temporary workspace. Cancellation can interrupt queued waits. These tokens account for admitted compiler work; they are neither CPU affinity nor dedicated physical cores. [Queue and compile entry points](../src/decompnet/compiler.py); [build command normalization](../src/decompnet/forks/project.py).

Six Deep workers against eight tokens, or ten workers against twelve tokens, leave potential capacity for other work, **not two priority-reserved agent tokens**. FIFO can put an agent behind earlier requests, and a multi-token request can block smaller requests behind it while waiting for enough free tokens. The module-global queue does not coordinate independent app processes, unrelated terminal compiles, or remote machines. These are direct consequences of the inspected implementation, not measured latency guarantees.

## Reading macOS CPU statistics

Apple specifies this M5 Pro as **six super cores plus twelve performance cores**; calling the twelve cores “efficiency cores” applies the wrong hardware description. [Apple specifications](https://www.apple.com/macbook-pro/specs/).

Keep measurement scales separate. A process CPU percentage in the usual one-core time scale treats one fully occupied CPU as about 100%; six fully busy compiler processes total about 600%, equivalent to roughly 33% of eighteen cores' aggregate time. This is an accounting illustration, not an expected measurement: staging, waiting, linking, candidate generation and Python coordination interrupt compiler execution. Aggregate System/User/Idle percentages describe the whole machine; Apple's CPU History view exposes processor activity. [Apple Activity Monitor guide](https://support.apple.com/guide/activity-monitor/view-cpu-activity-actmntr43452/mac); local macOS `man top`, DISPLAY and event-counting sections.

CPU time is not a percentage of maximum instruction throughput: equal busy time on unlike cores need not complete equal work, and frequency changes affect throughput. Consequently, low aggregate usage alone does not prove that eighteen-way compilation will improve performance. Correlate CPU activity with measured candidates/s and pipeline stages. Apple explicitly describes different core execution rates and recommends profiling bottlenecks. [Addressing CPU bottlenecks](https://developer.apple.com/documentation/xcode/addressing-cpu-bottlenecks).

Observe the compiler/emulator children as well as the Python parent. A single Python row misses work in short-lived iSH processes. `top` interval samples are preferable to interpreting a single `ps` snapshot: macOS `ps %cpu` is a decaying average over up to a minute, and the first noninteractive `top` sample has invalid per-process CPU deltas. Local primary sources: `man ps`, `%cpu`; `man top`, `-l` and event-counting sections. For deeper attribution, Instruments and `powermetrics` can show CPU/core or cluster activity, process CPU/QoS and thermal pressure, depending on available samplers. Local `man powermetrics` documents process-coalition accounting for exited processes, `--show-cpu-qos`, `--show-process-qos-tiers`, `--show-process-samp-norm`, and thermal sampling. Check `powermetrics --help` for this machine; no sampler was run for this note.

## Scheduling, QoS and sustained throughput

Current background and Deep trial threads request Utility QoS. Apple describes Utility as balancing performance and energy, and User Initiated as emphasizing responsiveness for explicit user work. QoS influences scheduling and core placement; it does not bind work to a named core. A manual performance mode could compare User Initiated, but actual subprocess QoS must be observed rather than assumed from the parent thread. Apple recommends dynamic distribution of numerous tasks across heterogeneous cores. [App QoS initialization](../src/decompnet/app/workers.py); [Apple tuning guidance](https://developer.apple.com/documentation/apple-silicon/tuning-your-code-s-performance-for-apple-silicon).

A higher cap allows more runnable work but guarantees neither higher clocks nor greater sustained throughput. Record thermal pressure alongside throughput and frequency/activity information; short bursts can miss sustained constraints. Apple's `ProcessInfo.thermalState` guidance calls for reducing resource use at higher thermal states. `powermetrics` thermal pressure is instantaneous at the end of a sample, so a single reading does not describe the whole interval. [Apple thermal-state guidance](https://developer.apple.com/documentation/foundation/processinfo/thermalstate-swift.property); local `man powermetrics`, Thermal section.
