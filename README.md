# RenpySwitch — Ren'Py 8.5.3 for Nintendo Switch homebrew

Runs Ren'Py 8.5 games (Python 3) on a Switch with custom firmware, from the
Homebrew Menu. Based on the work of uyjulian (renpy-switch), Otorhin /
knautilus (Renpy2Switch), jvrcruzGAMES (NXPython, CPython 3.14 for Switch)
and icosane.

* Engine: Ren'Py **8.5.3**, official source + a small Switch patch (`renpy.patch`)
* Python: **CPython 3.14** via [NXPython](https://github.com/jvrcruzGAMES/NXPython)
* Rendering: Ren'Py's GL2 renderer on OpenGL ES (Mesa/nouveau), video and audio via FFmpeg 7.1
* Input: Joy-Con / Pro Controller and the touch screen

## Using it

1. **Get the engine**: download the files of the latest
   [release](../../releases) (or the `renpy-switch` artifact of the latest
   successful [Actions](../../actions) run, or build it, below). It has
   `renpy-switch.nro` (the engine), `apk2switch.py` (the converter) and
   `hacbrewpack.exe` (for installable NSPs, see below).
2. **Get the game folder**:
   * PC game: the `game` folder of the Ren'Py game.
   * Android APK made with Ren'Py: convert it with `tools/apk2switch.py`:

     ```
     python apk2switch.py game.apk --nro renpy-switch.nro --name mygame --title "My Game"
     ```

     It extracts the game, removes the Android `x-` prefixes, adds the
     presplash and the Switch controls, checks image/video formats and gives
     the `.nro` its own title, version and icon (icon needs
     `pip install pillow`). The icon is the APK's launcher icon (found through
     the manifest, adaptive icons included), else the game's
     `gui/window_icon.png`, or any picture given with `--icon`.

     Add `--pack` to put images, audio and video into a few `.rpa` archives
     (Ren'Py's own format; scripts stay loose). Thousands of small files are
     very slow to copy over USB/MTP; a handful of archives is not. An already
     converted folder can be packed in place:
     `python tools/apk2switch.py path/to/game --pack`.
3. **Copy to the SD card**:

   ```
   sdmc:/switch/mygame/mygame.nro
   sdmc:/switch/mygame/game/...
   ```

4. Start the Homebrew Menu **by holding R while launching any game**
   (full memory mode; the album applet does not have enough RAM), then
   start the game.

Saves, logs (`log.txt`, `traceback.txt`, `stdout.txt`) and caches go to
`sdmc:/switch/mygame/save/`.

### Installable NSP (HOME menu)

`--nsp` also makes `mygame.nsp`: a normal title with its own icon and name on
the HOME menu, started without the Homebrew Menu and with the full memory of
a game. The engine and the game are inside it, so no game folder is needed
on the SD card.

```
python apk2switch.py game.apk --nro renpy-switch.nro --name mygame --nsp
```

* Needs `hacbrewpack.exe` (in the artifact) next to `apk2switch.py` and
  `prod.keys` dumped from **your own console** (Lockpick_RCM), next to
  `apk2switch.py` or in `%USERPROFILE%\.switch\`. The keys stay on your PC.
* Install with DBI, Tinfoil, Goldleaf…; the console needs sigpatches.
* The title id is made from `--name` (`01f…000`): a rebuilt NSP of the same
  game keeps it, so it replaces the installed one (if the installer refuses,
  delete the old one first). `--title-id` sets another.
* Saves go to `sdmc:/switch/mygame/save/`, the same folder as the `.nro`
  version, so both share saves.
* An already converted folder works too:
  `python apk2switch.py sdmc-copy/switch/mygame/game --nro renpy-switch.nro --nsp`.

### Controls (`zz_switch_controls.rpy`)

| Button | Action |
|---|---|
| A | next line / select |
| B | back / game menu |
| X, Minus | hide the text box |
| Y | history |
| ZR | skip on/off |
| ZL | auto mode on/off |
| L / R | quick save / quick load |
| Plus | game menu |
| Right stick ↑ / ↓ | previous line (rollback) / next line |
| Left stick, D-pad | move between buttons |
| Touch | tap = next/press, long press = menu, swipe ↑ = history, swipe ↓ = hide text box |

Handheld mode uses Ren'Py's `touch`/`small`/`phone` screen variants (like an
Android phone); docked mode uses `tv`/`large`. All modes also set the
`switch` and `mobile` variants.

### Formats

The Switch's SDL_image 2.0.4 decodes PNG, JPEG and WebP. AVIF, JPEG XL and SVG
images are not supported: `apk2switch.py --convert-avif` converts AVIF in
place. Video/audio: anything FFmpeg 7.1 decodes (WebM VP8/VP9/AV1, Ogg,
Opus, MP3...). 3D models (`GLTFModel`) and Live2D are not supported.

## Building

The build runs in the `devkitpro/devkita64` container. The easiest way is to
push to your fork: the GitHub Actions workflow runs `setup.bash` and
`build.bash` and uploads the `.nro`. To set the Homebrew Menu title, run the
workflow manually (Actions → RenpySwitch Build → Run workflow).

Locally (Linux, WSL2 or Docker):

```
docker run -it --rm -v "$PWD":/work -w /work devkitpro/devkita64:latest bash
bash setup.bash
bash build.bash
```

Pushing a tag `v*` (e.g. `v8.5.3-switch.2`) also publishes the files as a
GitHub Release, which downloads without a login (build scripts of game ports
pin a release tag).

Output in `raw/`: `renpy-switch.nro`, `renpy-switch.elf`, `exefs/main` (NSO),
`exefs/main.npdm` and `romfs/`. `scripts/nsp_tools.bash` then builds
hacbrewpack (Linux and Windows) and checks the NSP path end to end: the code
apk2switch takes from the `.nro` must equal `elf2nso`'s, and a test NSP made
with throwaway keys is unpacked again with hactool and checked.

### How it is put together

* `setup.bash` — devkitPro portlibs (SDL2, FFmpeg, HarfBuzz, FriBiDi, Mesa…),
  NXPython, Ren'Py 8.5.3 source (patched with `renpy.patch`) and SDK.
* `scripts/switch_modules.py` — runs Ren'Py's own Cython build helpers in
  static mode and generates the C sources, the CMake source list and the
  `inittab` for all 60 Ren'Py extension modules.
* `switch/` — the launcher (`main.c`, CPython embedding via `PyConfig`) and
  the CMake project that links everything into one executable.
* `build.bash` — compiles, precompiles `renpy/common` with the official SDK,
  stages the RomFS (Ren'Py package + CPython stdlib + `six`/`ecdsa` +
  `_posixsubprocess` stand-in), precompiles bytecode and packs the `.nro`.

### What `renpy.patch` changes

* `renpy.switch` platform flag (`sys.platform` is `nx` under NXPython),
  `renpy.mobile` on the Switch.
* Paths: game from RomFS or next to the `.nro`; saves, logs, persistent data,
  MultiPersistent and regenerated caches on the SD card (a writable overlay
  that comes first in the search path).
* GLES2 renderer, fullscreen, mobile redraw behaviour, full-screen presplash
  with CPU boost while loading.
* Screen variants for handheld/docked.
* `renpy.gl2.assimp` replaced by a pure-Python stand-in (no assimp on the
  Switch), save sync disabled, `posix_memalign` avoided in `ffmedia.c`.

### Testing without a console

`scripts/simulate_switch.py` runs the patched engine on desktop Linux in
"Switch mode": `sys.platform = "nx"`, the CPython modules NXPython does not
build are blocked, and the game/save paths are passed like on the console.
Ren'Py's Cython modules must be built for the host Python 3.14 first
(`python setup.py build_ext --inplace` in the patched source).

## Notes

* No game data is included or built in CI — supply your own.
* No keys are included or used in CI: NSPs are made on your own PC with your
  own console's keys.
