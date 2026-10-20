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

1. **Get the launcher**: download the `renpy-switch-nro` artifact from the
   latest successful [Actions](../../actions) run (or build it, below).
2. **Get the game folder**:
   * PC game: the `game` folder of the Ren'Py game.
   * Android APK made with Ren'Py: convert it with `tools/apk2switch.py`:

     ```
     python tools/apk2switch.py game.apk --nro renpy-switch.nro --name mygame --title "My Game"
     ```

     It extracts the game, removes the Android `x-` prefixes, adds the
     presplash and the Switch controls, checks image/video formats and gives
     the `.nro` its own title and icon (icon needs `pip install pillow`).
3. **Copy to the SD card**:

   ```
   sdmc:/switch/mygame/mygame.nro
   sdmc:/switch/mygame/game/...
   ```

4. Start the Homebrew Menu **by holding R while launching any game**
   (full memory mode; the album applet does not have enough RAM), then
   start the game.

Saves, logs (`log.txt`, `traceback.txt`, `stdout.txt`) and caches go to
`sdmc:/switch/mygame/save/`. The game folder can also be embedded into the
`.nro` (`pack_nro.bash --embed`), but a loose folder avoids the FAT32 4 GB
file limit and allows updates by copying files.

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

Output in `raw/`: `renpy-switch.nro`, `renpy-switch.elf`, `exefs/main` (NSO)
and `romfs/`.

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
* An installable `.nsp` needs proper NCA packaging (e.g. hacbrewpack with your
  own keys) and is not provided.
