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

- `30295781-…` control: write commands; **every** notification comes here,
  bulk data included.
- `30295782-…` status (read): four u32 LE, used bytes, unread packets, free
  bytes, clock valid.

The pendant keeps a ring buffer of audio packets addressed by sequence
number. Commands (first byte; integers big-endian):

| Command | Bytes | Meaning |
|---|---|---|
| `0x10` ring info | 1 | ask what the ring holds (answered with an info notification) |
| `0x11` ring read | 9 or 13: seq (u64), optional count (u32) | stream packets from seq (all, without a count) |
| `0x12` ring advance | 9: seq (u64) | mark everything before seq as received, freeing it |
| `0x13` ring clear | 1 | discard the ring |
| `0x03` stop | 1 | stop a transfer |

Notifications start with a type:

| Type | Rest |
|---|---|
| `0x01` ack | status u8; errors include 6 invalid command, 9 storage not ready (the SD card remounts for up to about 5 s after a connection; the pendant waits that long before answering), 10 sequence out of range |
| `0x02` info | read seq u64, write seq u64, capacity u32, dropped u64, packet size u16 (444) |
| `0x05` read begin | start seq u64, packet count u32 |
| `0x03` data | the next bytes of the packet stream, cut at the MTU without regard to packet boundaries |
| `0x04` done | status u8, next seq u64 |

A packet is 444 bytes: the pendant's UTC time in whole seconds (u32 BE), taken
when the packet filled, then 440 bytes of `[len u8][Opus frame]...`. Zero is
padding. When a frame doesn't fit, the firmware writes its length byte with
no frame (the frame starts the next packet) and leaves stale bytes after it,
so a frame that would reach the end ends the packet. The codec is the live
one (`19B10002`).

Behaviour that matters (`storage.c`, `sd_card.c`, `transport.c`, `rtc.c`):

- Audio is stored only while no phone is connected, and only while the
  pendant's clock is valid. While connected but not subscribed to audio, it
  is thrown away, not stored.
- The pendant frees packets as their notifications finish sending (every 2 s,
  and at done or disconnect); the advance command isn't needed. So the phone
  must spool on arrival: there's no second copy.
- After an unclean reboot the clock restarts from the last time a phone set
  it, so timestamps can run early until the next connect sets it again.

Hearsay's app (`ios/Hearsay/Pendant.swift`) asks for info on every connect,
reads everything, cuts the stream into packets and spools each one;
`hearsay/capture.py` places them.

## Not needed for now

Accelerometer (`accel.c`) and speaker playback.
