# PS2 matching tools and compiler experiments

Research date: 2026-11-18. Scope: the owner's SCUS-97328 matching workflow on Apple Silicon, using decompdecomp's PS2 ROM judge. This is a source-backed integration assessment, not a benchmark or a finding that one compiler built the entire game. No game data or compiler binaries were downloaded for this research.

## Recommendation

Prioritize better experiment selection around the existing byte judge: classify the residual difference, test the small set of plausible compiler/assembler profiles, then run bounded source variations. Another general decompiler is less likely to resolve the last register-allocation differences than a reproducible experiment ledger and lifetime-aware source changes. This is an engineering inference from the existing integration and upstream tool capabilities, not a measured speedup.

Use names recovered by SDK scanning and companion comparison as evidence leads. Only the app's linked byte comparison establishes a ROM match. Keep reference pseudocode, signature identity, source provenance and a verified matching implementation distinct.

## Changes for the current project

- The live project already assigned `sceRead` at `0x01024BB8–0x01024E24` to `ee-gcc-2.9-991111 -O2 -G0`. The function was still open when the pin was checked.
- All 11 PS2 compiler profiles in the app's catalog, the three PS2 analysis tools, m2c, Ghidra, MIPS binutils and decomp-permuter were installed on the GT4 host. The boot ELF and `CORE.elf` had no symbol table or debug sections usable by CCC. The existing profiles and analysis tools cover the immediate investigation.
- Helpers now have `compare_compilers` in ROM projects. One call ranks the assigned settings and up to two alternatives on the same saved C. It uses the normal linked-byte judge. `try_compiler` still provides the full diff. See [compiler engine](../src/decompnet/app/engine/compilers.py), [MCP tools](../src/decompnet/app/tools.py) and [API helper schemas](../src/decompnet/agents/api.py).
- The app caches SDK scans and companion comparisons in memory. The cache keys include the executable contents and tool revision. Companion comparisons also include the function catalog. On one read-only run against this project, the SDK scan took 0.246 seconds cold and 0.003 seconds cached. The companion comparison took 2.164 seconds cold and 0.009 seconds cached. Both repeat calls returned identical reports. These measurements cover the two lookups on this Mac. See [PS2 analysis](../src/decompnet/consoles/ps2/analysis.py).
- The PS2 importer now prefers the standard EE-GCC 3.2 profile when the standard driver and 040921 driver tie in the import sample. The installed-tool import tests exercise both explicit build profiles and compiler detection. See [PS2 profile order](../src/decompnet/consoles/ps2/__init__.py) and [PS2 tests](../tests/test_ps2.py).
- All eight working helpers resumed after the restart. Their tool lists included compiler comparison, flag search, the compiler map, all three PS2 analysis calls, redrafting and the byte judge. Project instructions now tell helpers when to use each tool.
- A read-only Codex helper called `project_info`, `compare_compilers`, `try_compiler`, `ps2_decompile`, `ps2_sdk_symbols` and `ps2_symbol_transfer` through the live app. Server counters recorded all six calls, and the helper reported results for each. On the current saved C for `sceRead`, the assigned 2.9 profile and the 2.9 plus 2.96 assembler profile scored 1,970 each. The 2.96 comparison scored 3,507. The SDK scan found an `sceRead` candidate and the companion comparison reported a medium-confidence counterpart. The comparison left the function open.

## Existing integration, checked in this checkout

Local sources are authoritative for what this checkout exposes; they do not prove that an already-running installed app has loaded these changes.

| Capability | Existing route or owner | Practical use and limit |
| --- | --- | --- |
| Linked byte judge | `compile_and_diff`; [rom/match.py](../src/decompnet/rom/match.py) | Final authority for submitted C linked at the real address. |
| Compiler and flags | `project_info`, `compiler_map`, `search_flags`, `compare_compilers`, `try_compiler`, `suggest`; [app/tools.py](../src/decompnet/app/tools.py) | Rank a small set of profiles and inspect the full diff for a promising one. A proposed range needs neighbouring evidence. |
| R5900 pseudocode | `ps2_decompile(function, offset=0)` | Optional installed Emotion Engine extension; paged text, useful for semantics and EE instructions. |
| SDK names | `ps2_sdk_symbols(function='', offset=0)` | Optional scanner, with a function filter or paged project output. Name candidates do not establish compiler or source identity. |
| Companion counterparts | `ps2_symbol_transfer(function='', offset=0, companion='CORE.elf')` | Companion must already be in project `analysis-inputs`; returns candidate correspondence, not matching C. |
| m2c drafts | `get_function`, `redraft`; [app/engine/briefs.py](../src/decompnet/app/engine/briefs.py) | Drafts depend on compiler target and relevant shared-header declarations. Correct types before redrafting. |
| Background permutation | [app/permute.py](../src/decompnet/app/permute.py), [permuter_gen.py](../src/decompnet/permuter_gen.py) | Uses upstream randomizer to generate candidates; the app's own judge scores them. No dedicated helper MCP permutation call was found in `app/tools.py`. |
| Splitting/disassembly | [rom/split.py](../src/decompnet/rom/split.py), splat/spimdisasm | Existing import foundation; boundary repair is more useful than replacing it for an isolated near-match. |

The PS2 analysis wrapper validates the imported ELF hash, limits concurrent helper analyses to two, and returns 5,000-character pages. Ghidra currently imports/analyzes in a temporary project for a request; reusing analysis by ELF hash is a plausible latency improvement, but requires a new cache/invalidation implementation. See [analysis.py](../src/decompnet/consoles/ps2/analysis.py) and [analysis_tools.py](../src/decompnet/consoles/ps2/analysis_tools.py).

## Tools worth using or extending

### 1. decomp-permuter: target the final allocation differences

Upstream supports random transformations and explicitly enumerated alternatives through `PERM_GENERAL`, `PERM_LINESWAP`, and related macros. It recommends permutation late, when allocation differences remain, and acknowledges weaker handling of reorderings and stack differences. Its stock score may ignore stack positions; that score must not replace the linked byte judge. Python and local compiler wrappers suit this setup. The existing app uses its randomizer rather than delegating final acceptance. Adding a small, bounded manual-alternative runner would be new integration; do not assume upstream PERM macros already work through the helper API. [Upstream README](https://github.com/simonlindholm/decomp-permuter/blob/main/README.md).

Suggested experiments: split or merge a temporary; change the order independent values become live; alter declaration/use scope; compare pointer walking with indexed access; compare equivalent branch structures. These are hypotheses, not guaranteed compiler switches. Preserve types and observable semantics; inspect any successful mutation for accidental dependence on undefined behavior.

### 2. m2c: improve types and stack evidence before generating more C

m2c explicitly supports `mipsee` with little-endian eabi64, compiler-specific heuristics, context types and stack-struct templates. It targets matching C but admits imperfect output, particularly loops. Existing PS2 integration uses `mipsee-gcc`; the local console notes no dedicated CodeWarrior MIPS target. Stack-struct and additional output options would need explicit app plumbing if helpers are to request them. Use m2c and Ghidra as independent interpretations of difficult control flow. [Upstream README](https://github.com/matt-kempster/m2c/blob/master/README.md), [local PS2 connector](../src/decompnet/consoles/ps2/__init__.py).

### 3. Ghidra Emotion Engine: Reloaded: keep the EE-aware implementation

Reloaded supports EE-specific MMI and VU0 macro instructions, `.mdebug` type recovery, constant-reference analysis and PCSX2 save-state import. The original beardypig extension is archived; replacing Reloaded with that ancestor would lose a maintained path. Current app pins Reloaded 2.1.39 for its Ghidra build. These are reference-analysis capabilities, not byte-matching compilers. The upstream workaround for some failed decompilations is disabling Decompiler Parameter ID. [Reloaded README](https://github.com/chaoticgd/ghidra-emotionengine-reloaded/blob/master/README.md), [archived original](https://github.com/beardypig/ghidra-emotionengine), [local pin](../src/decompnet/consoles/ps2/analysis_tools.py).

### 4. PS2Recomp SDK scanner: library triage, not compiler proof

The upstream analyzer's database derives from symbol-bearing games and samples retaining relocations. It can miss unrepresented SDK variants and deliberately ignores ambiguous hits. Its Ghidra integration is preferred over the heuristic JAL scanner for stripped games. Existing helper scanning is therefore useful for narrowing a function to a library family, including an `sceRead` candidate, but cannot alone identify SDK revision, compiler flags, source spelling, or exact boundary. PS2Recomp's full native-port recompiler serves a different objective and should not replace the matching judge. [Pinned upstream analyzer documentation](https://github.com/ran-j/PS2Recomp/blob/e5636981a1c9de368f6b71e819c3dc188dfedbf4/ps2xAnalyzer/Readme.md).

For library reconstruction, compare independently available open PS2SDK code or the owner's lawful SDK input, retaining revision and license receipts. Existing [ps2sdk.py](../src/decompnet/consoles/ps2/ps2sdk.py) verifies selected relocation-free kernel routines from pinned open sources; this is not a general proof that a retail library is identical to today's PS2SDK.

### 5. Chaos Compiler Collection: highest value when symbols exist

CCC parses PS2 debug symbols, including STABS `.mdebug`, ELF symbols and SNDLL linker symbols. `stdump` emits types/functions/globals as C++ or JSON; its demangler handles GCC2 and GCC3+ name schemes. This could improve companion-derived prototypes or library boundaries if the owner's files retain those sections. It cannot recreate stripped information by itself. Native CMake builds are a reasonable Apple Silicon candidate, but were not built here. Integration would require pinned installation, parsing and provenance-aware import; the existing companion MCP route only performs shape transfer. [CCC README](https://github.com/chaoticgd/ccc/blob/main/README.md).

### 6. decomp-compat-kit, asm-differ and objdiff: selective reuse

The compat kit already supplies the pinned symbol-transfer script. Upstream also advertises compiler fingerprinting and a matching loop. Evaluate those as advisory triage against known matched functions before adding them; overlapping workflows risk two inconsistent judges. [Upstream kit](https://github.com/LinuxJessi/decomp-compat-kit), [local tool installer](../src/decompnet/consoles/ps2/analysis_tools.py).

asm-differ provides a MIPS assembly viewer and watch/rebuild loop; objdiff is a modern object-diff alternative. They can improve a human's side-by-side inspection, but adding another UI is lower priority than returning structured residual classes through existing helper calls. Their upstream MIPS claims do not establish complete R5900/MMI coverage; test EE instructions with small fixtures before adopting a scorer. [asm-differ README](https://github.com/simonlindholm/asm-differ/blob/master/README.md), [objdiff](https://github.com/encounter/objdiff).

splat already supports PS2 and spimdisasm supplies MIPS analysis. Keep them for extraction and boundaries; neither supplies a compiler's original register choices. [splat](https://github.com/ethteck/splat), [spimdisasm](https://github.com/Decompollaborate/spimdisasm).

## Compiler/profile options on Apple Silicon

The local catalog already distinguishes profiles below. Their presence means available configuration, not installation status or proof of historical use in SCUS-97328. Sources: [toolchains.toml](../src/decompnet/toolchains.toml), [PS2 connector](../src/decompnet/consoles/ps2/__init__.py).

| Profiles | Why test them | Host route recorded locally |
| --- | --- | --- |
| `ee-gcc-2.9-991111`, `ee-gcc-2.9-991111-01` | Different period build; local catalog records divide-padding distinction. | Pinned Linux/i386 tools via Linux VM/runtime. |
| `ee-gcc-2.9-as296` | Same compiler generation, different assembler scheduling. | Linux VM; depends on 2.96 assembler. |
| `ee-gcc-2.96`, `ee-gcc-2.96-as2004` | Separate code generation from later unaligned-access macro expansion. | Linux VM; hybrid depends on 040921 assembler. |
| `ee-gcc-3.2`, `ee-gcc-3.2-040921` | Same archive hash, different driver invocation; avoid treating them as independent compiler evidence. | Linux VM. |
| `ee-gcc-2.95.2`, `ee-gcc-2.95.3` | SN Systems GCC variants for regions that demonstrably fit them. | Windows executables through pinned wibo. |
| `mwcps2-2.3.3`, `mwcps2-3.0.1` | Alternative compiler family only when code shape and sample matches support it. | Proprietary Windows tools through wibo; require accepted setup. |
| Owner-supplied `own-ps2` kit | Missing authentic build, when available lawfully. | Explicit kit setup; verify executable, host support, hashes and invocation. |

A profile is authoritative for a particular candidate only when its actual compiled-and-linked bytes match. A compiler-family fingerprint is preliminary. Compiler, assembler, driver, ABI, include environment and small-data settings must be recorded together. Test `-G0` versus `-G8` only with correct global sizes and `$gp`; arbitrary small-data changes can hide a bad declaration.

Modern ps2dev is useful for homebrew probes and reference behavior. Its current configuration points to GCC `ee-v15.2.0` and binutils `ee-v2.45.1`, and its build script explicitly accommodates Apple Silicon dependencies. That does not make it a drop-in substitute for period EE-GCC in a matching task. Likewise generic old-gcc `mipsel` builds are not proof of Sony EE patches or the EE ABI. [ps2dev configuration](https://github.com/ps2dev/ps2toolchain-ee/blob/main/config/ps2toolchain-ee-config.sh), [host build script](https://github.com/ps2dev/ps2toolchain-ee/blob/main/scripts/002-gcc-stage1.sh), [old-gcc scope](https://github.com/decompals/old-gcc/blob/master/README.md).

## Bounded workflow for a near-match

1. Save the best source, target bytes/hash, header hash, function boundary, active profile and linked diff. Verify whether the mismatch is calls/globals, instructions, stack layout, scheduling or registers.
2. Correct prototypes, integer widths, signedness, struct offsets and small-data declarations first. Compare the existing m2c draft with EE-aware Ghidra only where semantics remain uncertain.
3. Rank plausible profiles with `compare_compilers`. Use `try_compiler` for the full diff of a promising profile and `search_flags` for a specific flag hypothesis. Separate assembler-sensitive macros and delay slots from compiler-generated register allocation.
4. For an SDK lead, obtain `ps2_sdk_symbols` and any companion counterpart. Confirm size, callers, referenced data and instruction sequence. Keep the recovered name provisional until corroborated.
5. Freeze the profile and header for a bounded C-idiom search. Cache by source/header/profile hash and retain failures as well as best candidates. Record transformation, compile duration and exact residual differences. Retry a failed experiment only when an input changes.
6. Apply random permutation to allocation-only residuals. Keep every acceptance behind `compile_and_diff`; any external score is ranking evidence. For a compiler-range proposal, test neighbouring nontrivial functions before broadening it.

Steps 1–4 mostly use existing helpers. A persistent experiment ledger, automatic residual classifier, register-lifetime explanation, manual PERM alternatives and Ghidra analysis reuse are proposed integrations, not features established by this investigation.

## Licensing, provenance and evidence limits

Upstream labels m2c GPL-3.0, decomp-permuter MIT, spimdisasm/splat MIT, the original Ghidra extension Apache-2.0 and objdiff MIT/Apache-2.0. CCC's own code is MIT but its GNU demangler includes GPL/LGPL files. Inspect the pinned release's complete notices before redistributing an integration; the umbrella tool license does not establish the rights to every embedded signature or compiler archive. [m2c](https://github.com/matt-kempster/m2c), [permuter](https://github.com/simonlindholm/decomp-permuter), [CCC notices](https://github.com/chaoticgd/ccc/blob/main/README.md), [objdiff](https://github.com/encounter/objdiff).

Do not infer redistribution permission for commercial SDK libraries or CodeWarrior from their availability in a public archive. Use existing accepted installations or explicitly owner-provided kits. Do not upload game executables to online decompilation services to test these recommendations. Keep recovered proprietary data outside the repository.

The timings above measure repeated symbol lookups. Match throughput and `sceRead` byte equality require separate measurements over the helpers' subsequent work.
