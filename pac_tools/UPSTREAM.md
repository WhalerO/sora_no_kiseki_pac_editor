# PAC fallback tools: upstream and license

The fallback files in this directory were supplied for interoperability with
the FPAC archives used by *Trails in the Sky 1st Chapter*.

- Upstream project: <https://github.com/eArmada8/kuro_dlc_tool>
- Upstream source files:
  - `sky_extract_pac.py` (locally named `extract_pac.py`)
  - `sky_create_pac.py` (locally named `create_pac.py`)
- Upstream license: GNU General Public License v3.0
- License text: <https://github.com/eArmada8/kuro_dlc_tool/blob/main/LICENSE>

Only the auditable Python sources are retained. In a frozen build the main
application starts a headless child instance of itself, which loads one of
these scripts in that separate process. No opaque upstream executable is
distributed. The normal TIS_Retext PAC path uses the independently maintained
service in `retext/archive/` and does not automatically invoke the fallback.

When redistributing a build that contains this directory, retain this notice,
the corresponding Python source, and the complete upstream GPL-3.0 license.
