# Day-to-day procedures

A cheat sheet for the things that are easy to forget between sessions.
Commands assume you are in the repository root.

## Setup, once

PlatformIO keeps its tools out of the way, so `pio`, `wlink` and the RISC-V
binutils are not on the path by default. Add this to `~/.bashrc` and the
commands below work as written:

```bash
export PATH="$HOME/.platformio/penv/bin:\
$HOME/.platformio/packages/tool-wlink:\
$HOME/.platformio/packages/toolchain-riscv/bin:$PATH"
```

Then `source ~/.bashrc`, or open a new terminal. Without it, spell the paths
out in full — for example `~/.platformio/penv/bin/pio run`.

## Change something and publish it

```bash
pio run                      # does it still build?
git add -A
git commit -m "what changed and why"
git push
```

That is all a code change needs. CI builds the firmware and both executables on
every pull request and tag, but **a plain push does not make a release**.

## Cut a release

```bash
git tag v1.2
git push --tags
```

Two minutes later the [Releases page](../../releases) has a new entry with
`si5351-tool` and `si5351-tool.exe` attached, built natively on GitHub's own
Linux and Windows machines. That link is what you hand to someone else.

Check it went through:

```bash
gh run list --limit 1                 # is it still running?
gh release view v1.2                  # what got attached
```

Version numbers are arbitrary — bump the second digit for ordinary changes.

### If a release build fails

```bash
gh run list --limit 3                 # find the run id
gh run view <id> --log-failed         # read only the failing step
```

Fix, commit, push, then move the tag onto the fixed commit:

```bash
git push --delete origin v1.2
git tag -d v1.2 && git tag v1.2
git push --tags
```

Moving a tag is only safe while nobody has pulled it — which is true for the
first few minutes after tagging, and not much longer.

## Flash the firmware

```bash
pio run -t upload
```

**This erases the stored frequency.** The upload wipes the whole flash,
including the config page, so an updated board comes up on the built-in
fallback until you send a sequence again.

### If the probe cannot see the chip

`Probe is not attached to an MCU` means something is driving package pin 8,
which the debug line shares with the hardware UART TX. Current firmware leaves
that pin alone, so this should not happen — but if it does, build minichlink
once and unbrick:

```bash
cp -r ~/.platformio/packages/framework-arduinoch32v003/ch32v003fun ~/ch32v003fun
cd ~/ch32v003fun/minichlink && make minichlink
./minichlink -u                       # erases the flash, frees the pin
cd -
pio run -t upload
```

## Set a frequency on a board

```bash
python3 tools/si5351_tool.py          # or run dist/si5351-tool
```

Pick the port, **Connect**, type the frequency (`10.245 MHz`, `32.768k` and
`3579545` all work), then **Send sequence**. The controller answers, so the
status line tells you whether it worked.

If the port is missing from the list, press **Rescan** — unplugging the adapter
moves it between `/dev/ttyACM0` and `/dev/ttyACM1`.

To trim against a counter: tick **Auto-send on change**, turn the correction
spinner until the reading matches, then press **Send sequence** once to commit.
Auto-send never writes to flash, so tuning costs no write cycles.

## Change what an unconfigured board comes up on

Tick **Advanced**, set the frequency you want, press **Set boot-time fallback
in main.cpp**, then `pio run -t upload`. This only matters for a board that has
never been set, or one you have just re-flashed.

## Rebuild the executables locally

```bash
./tools/build_app.sh linux
./tools/build_app.sh windows          # needs docker
```

Rarely needed — CI does this on every tag. Useful for trying a change before
tagging.

## Check the board without the tool

The controller records its state in RAM for the debug probe. Addresses move
between builds:

```bash
riscv-wch-elf-nm .pio/build/genericCH32V003J4M6/firmware.elf \
  | grep -E "si5351_ok|cfg_from_flash|i2c_diag"
wlink --chip CH32V003 dump 0x2000000C 16
wlink --chip CH32V003 dump 0x08003C00 64    # the stored sequence
```

A healthy board reads `si5351_ok = 01` and `i2c_diag_fail_cnt = 00`. The full
table is in [NOTES.md](NOTES.md).

## If GitHub refuses a push

```
refusing to allow an OAuth App to create or update workflow ... without `workflow` scope
```

The login token may not create CI files. Fix it once:

```bash
gh auth refresh -h github.com -s workflow
```

Press Enter, paste the code it prints into <https://github.com/login/device>,
and **leave the command running** until it says `Authentication complete` — it
has to collect the new token itself. Authorising in the browser alone does
nothing.
