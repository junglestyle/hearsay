# Omi pendant over BLE

What the pendant's firmware exposes, for the iPhone app (slice 9). Read from
Omi's open-source firmware (BasedHardware/omi, MIT), `omi/firmware/omi/src/`,
at commit 1de6013 (2026-10-01). Re-check against the firmware when something
doesn't behave as described; firmware updates can change it.

The pendant accepts one connection at a time: whichever app holds it gets
everything, and the others get nothing.

## Discovering what a pendant has

Features service `19B10020-E8F2-537E-4F6C-D104768A1214`, characteristic
`19B10021-…` (read): a bitmask of what this pendant supports.

| Bit | Feature |
|---|---|
| 0 | speaker |
| 1 | accelerometer |
| 2 | button |
| 3 | battery |
| 4 | USB |
| 5 | haptic |
| 6 | offline storage |
| 7 | LED dimming |
| 8 | mic gain |

Device Information Service (standard, `0x180A`): model, firmware revision.

## Audio (in use)

Service `19B10000-E8F2-537E-4F6C-D104768A1214`.

- `19B10001-…` audio data (notify). Packets: 2-byte LE index, 1-byte frame
  id, then codec bytes; reassemble frames, drop the in-progress frame on a
  lost packet (`capture/`, `ios/Hearsay/Pendant.swift`).
- `19B10002-…` codec (read): 1 PCM8, 10 PCM16, 20 or 21 Opus (21 on current
  firmware).
- `19B10003-…` speaker (write): audio to play on pendants with a speaker.

## Time (in use)

Service `19B10030-…`: `19B10031-…` write 4 bytes LE uint32 epoch seconds;
`19B10032-…` read the pendant's current time.

## Button (in use)

Service `23BA7924-0000-1000-7450-346EAC492E92`, characteristic
`23BA7925-…` (notify), first byte:

| Value | Event |
|---|---|
| 1 | single tap |
| 2 | double tap |
| 3 | long tap |
| 4 | press |
| 5 | release |

Taps are what to map to actions; press/release are the raw edges.

## Haptic

Service `CAB1AB95-2EA5-4F4D-BB56-874B72CFC984`, characteristic
`CAB1AB96-…` (write, 1 byte): 1 = 100 ms, 2 = 300 ms, 3 = 500 ms. Other values
are ignored. Patterns (e.g. two short for unmute, one long for mute) are
sequences the app writes with pauses between them.

## Settings

Service `19B10010-E8F2-537E-4F6C-D104768A1214`.

- `19B10011-…` LED dim ratio (read/write, 1 byte): 0–100, saved on the pendant.
- `19B10012-…` mic gain (read/write, 1 byte): level 0–8, saved on the pendant
  and applied at once. **0 mutes the microphone in hardware**; 1 = −20 dB,
  2 = −10, 3 = 0, 4 = +6, 5 = +10, **6 = +20 dB (default)**, 7 = +30,
  8 = +40. A mute that survives an app crash must still be undone: read the
  level before muting and write it back on unmute.
- `19B10013-…` charging status (read/notify, 1 byte): 1 charging, 0 not.

Battery level: the standard Battery Service (`0x180F`, level `0x2A19`).

## Offline storage (audio recorded while no phone was connected)

Service `30295780-4301-EABD-2904-2849ADFEAE43`.

- `30295781-…` control (write commands; notify for acks and info).
- `30295782-…` data (read/notify).

The pendant keeps a ring buffer of audio packets addressed by sequence
number. Commands (first byte; integers big-endian):

| Command | Bytes | Meaning |
|---|---|---|
| `0x10` ring info | 1 | ask what the ring holds (answered with an info notification) |
| `0x11` ring read | 9 or 13: seq (u64), optional count (u32) | stream packets from seq |
| `0x12` ring advance | 9: seq (u64) | mark everything before seq as received, freeing it |
| `0x13` ring clear | 1 | discard the ring |
| `0x03` stop | 1 | stop a transfer |

Notifications on the control characteristic start with a type: `0x01` ack,
`0x02` info, `0x03` data, `0x04` done, `0x05` read begin. Error codes in acks
include 6 invalid command, 9 storage not ready (the SD card remounts for up to
about 5 s after a connection), 10 sequence out of range. For the exact layout
of the info and data notifications, read `lib/core/storage.c`
(`send_ring_info_response`, the data path) at the commit above.

For Hearsay: read, spool exactly as received along with whatever timing the
pendant provides (check how the data notifications place packets in time;
receipt time on the phone is not when the audio was recorded), then advance
only once the spool has it, so nothing is lost if the transfer breaks.

## Not needed for now

Accelerometer (`accel.c`) and speaker playback.
