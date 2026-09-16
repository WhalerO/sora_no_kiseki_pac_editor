# Bundled KuroTools resources

This directory contains the KuroTools-derived runtime pieces used by
TIS_Retext:

- `schemas/` and `lib/` for structured TBL parsing and repacking;
- `disasm/` for DAT structure verification and the experimental fallback roundtrip;
- `mdl/` as reference source for verified MDL format behavior;
- `processcle.py` and `support.py` for bounded CLE processing through
  zstandard and PyCryptodome.

The former standalone KuroTools CLI and generated example outputs are not part
of the application. TIS_Retext calls these resources through its engine
service boundary. See `UPSTREAM.md` for the original project documentation and
`LICENSE.md` for its MIT license.

The bundled Schema set includes the upstream Sora2 layouts currently available
for `BattleLevelEnemyAdjust`, `BattleLevelTurn`, `CharaArrange`,
`ItemTableData`, `MedalItem`, and `StatusParam`.  TIS_Retext can select the
Sora1/Sora2 family explicitly or infer it per TBL from exact record layouts.
The Sora2 Schema additions were synchronized from upstream commit
`2ad1174ffe64afe4bc39bb1060dc38bc5b68f354`.

TIS_Retext adds shared relocation layers around these resources:

- unknown DAT `OP_24` command names retain their numeric `(structID, opcode)`
  identity as `Cmd_unknown_XX_YY`, so an incomplete friendly-name dictionary
  does not hide later `PUSHSTRING` entries;
- TBL uses Schema fields plus validated unknown-header record columns. The
  pool-splice writer preserves fixed records and relocates only concrete
  64-bit external-offset fields, including internal string aliases;
- `#scp` DAT records every typed string pointer and splices the terminal pool
  without reassembling function bytecode. Rebuilds are still reread and
  compared against a non-text structure fingerprint.

DAT script assembly remains an experimental fallback; structured TBL and
`#scp` pool relocation do not require it. A successful text roundtrip is not
presented as proof of full game-runtime semantics; see
[text-engine boundaries](../../../docs/TEXT_ENGINE_BOUNDARIES.md).
