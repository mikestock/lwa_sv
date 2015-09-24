
# ADP ICD

## Document history

### 2015-08-10

* Changed definition of COR_NAVG from units of
  1/<math>f<sub>c</sub></math> to units of subslots due to
  implementation constraints.

* Re-named "TBW" --> "TBF" (Transient Frequency Buffer) due to
  significant deviation from DP's TBW mode.

* Added low-bandwidth TBN filters 1-4 to match DP spec.

* Changed DRX filters 1-4 to match DP spec.

### 2015-08-01

Initial mostly-complete draft.

## *TODO*

* *Finish RPT MIB entries*

* *New TBF output packet format for channelised data*

* *Update TBN and DRX packet formats with bits-per-sample info, new DRX_ID etc.*

* *Empirically confirm limits on input, computation and output
 rates. Can we capture ~45.6 MHz of bandwidth? Can we support 1.6 MHz
 TBN? Can we achieve 32 beams? How short can correlator integrations
 be? How much TBF data/time can we buffer?*

* Consider changing the word "recording" to "streaming".

* Add MIB entry for querying disk usage (primarily for TBN mode).

* Add discription/diagram of ADP network configuration.

## Observing modes

### TBN

Narrow-band recording of all inputs. Supports continuous recording of
all inputs at up to 1.6 MHz. This mode cannot be run concurrently with
any other modes.

### TBF

Wide-band recording of all inputs at low duty-cycle or on a
trigger. Supports recording of frequency-domain data for all inputs
across any subset of active DRX tunings for up to 10 seconds.

### BAM

The beamformer. Supports up to 32 dual-pol beams, each using any
tuning set by the DRX command, up to a maximum combined bandwidth of
39.2 MHz dual-pol. This mode can operate concurrently with TBF and
COR.

### COR

The correlator. Cross-correlates all 512 inputs using 25 kHz frequency
channels across any subset of frequency tunings set by the DRX
command. This mode can operate concurrently with TBF and BAM.

## Example command sequence

1. DRX -- *Set a tuning and reconfigure FPGAs with DRX parameters*
1. BAM 1 -- *Start a beam recording*
1. BAM 2 -- *Start another beam recording*
1. COR -- *Start the correlator recording*
1. BAM 1 -- *Change beam parameters*
1. TBF -- *Trigger a TBF dump of active tunings*
1. COR -- *Change correlator parameters*
1. STP "BAM2" -- *Stop the beam recording*
1. STP "COR" -- *Stop correlator recording*
1. STP "BAM1" -- *Stop the beam recording*
1. TBN       -- *Reconfigure FPGAs with TBN parameters and start TBN recording*
1. TBN       -- *Stop TBN recording, reconfigure FPGAs with new TBN parameters and start TBN recording again*
1. STP "TBN" -- *Stop TBN recording*
1. DRX -- *Set a tuning and reconfigure FPGAs with DRX parameters*
1. BAM 1 -- *Start a beam recording*
1. ...

## Fixed parameters

Symbol    | Value | Description
---       | ---   | ---
SUBSYSTEM | "ADP" | Name of the subsystem
FS (<math>f<sub>s</sub></math>) | 196.0 MHz | Signal sampling frequency unit*.
FC (<math>f<sub>c</sub></math>) | 25.0 kHz  | Width of each correlator (COR) frequency channel.

*<math>f<sub>s</sub></math> is not actually the sampling frequency
 used internally by ADP, but this definition is maintained for
 backwards compatibility with DP.

## Hardware

- `headnode`: The ADP cluster head machine, which is named `adp` (aka
  `adp0`). This machine runs the `adp-control` service and interfaces
  between the cluster and the outside (of ADP) world.

- `roaches`: The ADP cluster FPGA boards, of which there are 16, named
  'roach1-16'. These boards run the ADP `F-engine` firmware to
  frequency-channelise data from the ADCs and send it through the data
  switch to the servers.

- `servers`: The ADP cluster processing machines, of which there are
  six, named `adp1-6`. These machines run the `adp-pipeline` service
  (aka the `X/B/T-engines`), which captures and processes data streams from
  the roaches.

## MIB entries

Index    | Label                  | Type      | Bytes | Value(s) | Description
---      | ---                    | ---       | ---:  | ---      | ---
2        | `TBF_STATUS`           | `uint8`   | 1 | <ul><li>0: Idle.</li><li>4: Actively recording or writing out.</li></ul> | Current status of TBF.
3        | `NUM_TBN_BITS`         | `uint8`   | 1 | Always 16 (8 real + 8 imag) | No. bits per sample in TBN output. Currently always 16 (8 real + 8 imag).
4.1      | `NUM_DRX_TUNINGS`      | `uint8`   | 1 | Always 32 | Max no. frequency tunings.
4.2      | `NUM_BEAMS`            | `uint8`   | 1 | Always 32 | Max no. active beams.
4.3      | `NUM_STANDS`           | `uint16`  | 2 | Always 256 | No. stands.
4.4.1    | `NUM_BOARDS`           | `uint8`   | 1 | Always 16 | No. ROACH (FPGA) boards.
4.4.2    | `NUM_SERVERS`          | `uint8`   | 1 | Always 6 | No. servers.
??       | `NUM_FREQ_CHANS`       | `uint16`  | 2 | Always 4096 | Total no. correlator frequency channels.
4.5      | `BEAM_FIR_COEFFS`      | `uint8`   | 1 | Always 32 | No. FIR coeffs implemented.
4.6.n    | `T_NOMn`               | `uint16`  | 2 | Full range | T<sub>nom</sub>=L from LWA Memo 151, in units of samples at <math>f<sub>s</sub></math> for beam `n`.
5.1      | `FIR`                  | `sint16[16,32]` | 1024 | Full range | FIR coeffs for input specified by `FIR_CHAN_INDEX`.
5.5      | `FIR_CHAN_INDEX`       | `uint16`  | 2 | [1-512] | Returns and increments index of the input whose FIR coeffs are returned by `FIR`.
6        | `CLK_VAL`              | `uint32`  | 4 | [0:86401000) | Time at start of previous slot, in ms past station time midnight (MPM).
7.n.1    | `ANTn_RMS`             | `float32` | 4 | Full range | RMS power of `STAT_SAMP_SIZE` current samples for input `n`.
7.n.2    | `ANTn_DCOFFSET`        | `float32` | 4 | Full range | Mean of `STAT_SAMP_SIZE` current samples for input `n`.
7.n.3    | `ANTn_SAT`             | `float32` | 4 | Full range | No. saturated values (+-127) in `STAT_SAMP_SIZE` current samples for input `n`.
7.n.4    | `ANTn_PEAK`            | `float32` | 4 | Full range | Max of `STAT_SAMP_SIZE` current samples for input `n`.
7.0      | `STAT_SAMP_SIZE`       | `uint32`  | 4 | Typically 1024 | No. samples used to compute statistics.

## Control commands

### TBN command
#### Description

Configures and starts TBN mode.

TBN mode cannot be run while any other mode is running. Note that TBN
mode can only be started on a 1PPS boundary, so the `sub_slot`
argument is ignored. The command also requires any existing TBN
recording be stopped, and takes 1 second before recording can be
started again (i.e., it requires 1 second of downtime).

#### Arguments

Name           | Type                   | Value(s)   | Description
---            | ---                    | ---        | ---
`TBN_FREQ`     | `float32`              | 0-100e6    | Center frequency in units of Hz.
`TBN_BW`       | `sint16`               | 5-11       | Filter code indicating sample rate. See `DRX_FILTERn` codes below.
`TBN_GAIN`     | `sint16`               | 0-30       | Right-bitshift used to select which bits are output.
~~`sub_slot`~~ | `uint8`                | [0-99]     | Sub-slot at which to take effect.

#### Filter codes

`TBN_DRX_FILTERn` | Sample rate (kHz)
---       | ---:
1         |      1.000
2         |      3.125
3         |      6.250
4         |     12.500
5         |     25
6         |     50
7         |    100
8         |    200
9         |    400
10        |    800
11        |   1600

### DRX command

#### Description

Reconfigures a frequency tuning and enables TBF/BAM/COR
observing.

Note that this command can only be applied on a 1PPS boundary, so the
`sub_slot` argument is ignored. The command also requires all
TBF/BAM/COR recordings be stopped, and takes 1 second before
recordings can be started again (i.e., it requires 1 second of downtime).

Any combination of up to NUM_DRX_TUNINGS tunings totalling up to the
equivalent of 1x `DRX_FILTER8` (39.2 MHz) of bandwidth may be
specified. The specified tuning can be disabled by specifying `DRX_BW
= DRX_FILTER0`.

#### Arguments

Name           | Type                   | Value(s)   | Description
---            | ---                    | ---        | ---
~~`DRX_BEAM`~~ | `uint8`                | [1-NUM_BEAMS] | Beam to be changed.
`DRX_TUNING`   | `uint8`                | [1-NUM_DRX_TUNINGS] | Frequency tuning to be changed.
`DRX_FREQ`     | `float32`              | [0-102.1875e6] Hz | Center freq. in Hz.
`DRX_BW`       | `uint8`                | [3-8] or 0 | Filter code indicating sample rate. See `DRX_FILTERn` codes below. Zero disables the tuning.
`DRX_GAIN`     | `sint16`               | [0-15]     | Right-bitshift to compensate for BW reduction.
~~`sub_slot`~~ | `uint8`                | [0-99]     | Sub-slot at which to take effect.

#### Filter codes

`DRX_FILTERn` | Sample rate (kHz)
:---:     | ---:
0         | Disable
1         |   250
2         |   500
3         |  1000
4         |  2000
5         |  4900
6         |  9800
7         | 19600
8         | 39200

#### Constraints

The combined bandwidth of all active tunings must not exceed the
equivalent of 1x `DRX_FILTER8` (39.2 MHz). However, within this
constraint, any combination of `DRX_FILTERs` may be specified; e.g.,
1x `DRX_FILTER7` + 2x `DRX_FILTER6` + 4x `DRX_FILTER5` + 8x
`DRX_FILTER4` + 16x `DRX_FILTER3` is allowed.

This constraint arises due to network data transport limitations
between the roaches and the servers. Note that the similarity to the
BAM constraint is only a coincidence, as the two constraints stem from
different hardware limitations.

### TBF command
#### Description

Configures and starts a TBF capture.

Because ADP servers only have access to active tunings and not the
full band, TBF captures must specify which tunings to capture. Also as
a result of this constraint, ADP TBF output is in the form of
frequency channels (similar to the correlator but with no
time-averaging).

<b>See the COR command for more details about frequency
channels and the use of `DRX_TUNING_MASK`.</b>

#### Arguments

Name           | Type                   | Value(s)   | Description
---            | ---                    | ---        | ---
`TBF_BITS`     | `uint8`                | Must be 16 | No. bits per (complex) sample to output.
`TBF_TRIG_TIME`| `sint32`               | Full range | Trigger time since start of slot in units of <math>1/f<sub>s</sub></math>. Can be negative and/or multiple slots.
`TBF_SAMPLES`  | `sint32`               | [1-TODO]   | Length of time to output, in units of samples at <math>f<sub>s</sub></math>.
<b>`DRX_TUNING_MASK`</b> | `uint64`            | `NUM_DRX_TUNINGS` bits starting at MSB | Bit-mask specifying DRX tunings from which frequency channels are selected. MSB represents the 1<sup>st</sup> tuning.

### BAM command

#### Description
Configures and enables a beam recording with new delays and gains.

#### Arguments
Name           | Type                   | Value(s)   | Description
---            | ---                    | ---        | ---
`BEAM_ID`      | `sint16`               | [1-NUM_BEAMS] | Beam to be changed.
`BEAM_DELAY`   | `fixed16.4[512]`       | [0-256)    | Sample delay for each input.
`BEAM_GAIN`    | `fixed16.1[256][2][2]` | Full range | 2x2 polarisation mixing matrix for each stand.
<b>`DRX_TUNING`</b> | `uint8`     | [1-NUM_DRX_TUNINGS] | Frequency tuning to be used.
`sub_slot`     | `uint8`                | [0-99]     | Sub-slot at which to take effect.

#### Constraints

The combined bandwidth of all active beams must not exceed 39.2
MHz. However, within this constraint, any combination of beams and
tunings may be specified.

This constraint arises due to IO limitations for beamformed output
data. Note that the similarity to the DRX constraint is only a
coincidence, as the two constraints stem from different hardware
limitations.

### COR command
#### Description

Configures and enables the correlator.

The correlator generates
visibilities for all frequency channels that overlap with any DRX
tuning specified in the `DRX_TUNING_MASK` argument. E.g., setting
`DRX_TUNING_MASK` = `bitreverse64(0b1101)` (bitreversed due to
MSB-first ordering) tells the correlator to generate visibilities for
any frequency channel that overlaps with the first, third or fourth
active DRX tunings.

Correlator frequency channels have a width of
<math>f<sub>c</sub></math>, with channel <math>n &isin;
[0,`NFREQCHAN`-1]<math> centered at
<math>f<sub>n</sub>=n*f<sub>c</sub></math>. E.g., the first channel is
centered at 0 Hz (i.e., DC) and spans the frequencies
<math>[-f<sub>c</sub>/2, +f<sub>c</sub>/2</math>)</math>, the second
channel is centered at <math>f<sub>c</sub></math> and spans the
frequencies <math>[f<sub>c</sub>/2, 3f<sub>c</sub>/2)</math>, and so
on.

Note that due to the use of frequency channels and output data packets
of fixed size and alignment, the set of frequency channels actually
output by the correlator will typically be greater than the set
strictly required to satisfy the `DRX_TUNING_MASK` argument.

#### Arguments

Name           | Type                   | Value(s)   | Description
---            | ---                    | ---        | ---
`COR_NAVG`     | `sint32`               | >= 1000    | The integration time, in units of subslots.
`DRX_TUNING_MASK` | `uint64`            | `NUM_DRX_TUNINGS` bits starting at MSB | Bit-mask specifying DRX tunings from which frequency channels are selected. MSB represents the 1<sup>st</sup> tuning.
`COR_GAIN`     | `sint16`               | [0-15]     | Right-bitshift to compensate for BW reduction.
`sub_slot`     | `uint8`                | [0-99]     | Sub-slot at which to take effect.

#### Constraints

Due to output data rate limits, the value of `COR_NAVG` must obey
certain minimums as a function of the combined bandwidth of all DRX
tunings selected via the `DRX_TUNING_MASK` argument. Currently, only a
single global minimum of COR_NAVG >= 1000 (= 10.0 seconds) is
specified.

*TODO: Empirically determine lower limits on `COR_NAVG` as a function of correlated bandwidth.

### STP command
#### Description

Stops data output from an active observing mode.

#### Arguments

Name           | Type                   | Value(s)   | Description
---            | ---                    | ---        | ---
`DATA`         | `string`               | One of [`"TBN"`,`"TBF"`,`"BEAMn"`,`"COR"`] | The observing mode to stop.

#### INI command
#### Description

Re-initialises the ADP subsystem using the default configuration,
putting the system into the same state as it would be at power-up
prior to receiving any control commands.

In addition to re-loading the default configuration, this command also
re-loads the FPGA firmware and re-calibrates the ADC delays. If any
part of the system fails to re-initialise or re-calibrate, the ADP
`SUMMARY` MIB entry will be set to `"ERROR"` and the error condition
will be set in `INFO`.

#### Arguments

None

### FST command
#### Description

Configures the FIR filter coefficients to be applied to an input (or
shared by all inputs).

Note that internally, ADP converts the given coefficients into
frequency-domain weights to be applied to the complex specta of each
input.

#### Arguments

Name           | Type                   | Value(s)   | Description
---            | ---                    | ---        | ---
`INDEX`        | `sint16`               | -1, 0, or [1-512] | The input whose filter to change. -1 => load defaults; 0 => apply to all.
`COEFF_DATA`   | `sint16[16][32]        | Full range | Filter coefficients for each of 16 fine delays.

### SHT command
#### Description

Shuts down or resets the ADP subsystem, putting it into a low-power
state. All firmware is unloaded and all processing servers are shut
down.

Regular (soft) shutdown gives all active ADP processes the opportunity
to close cleanly. The `SCRAM` option instead causes issue of an
immediate hard shutdown, killing active server processes. The optional
`RESET` option causes all hardware to be brought back up immediately
after soft or hard shutdown.

#### Arguments

Name           | Type                   | Value(s)   | Description
---            | ---                    | ---        | ---
`DATA`         | `string`               | Optional `"SCRAM"` and/or `"REBOOT"` | The type of shutdown to issue.

## Correlator output interface

The packet data header shall contain the following entries:

Name           | Type     | Value(s)   | Description
---            | ---      | ---        | ---
`COR_NAVG`     | `sint32` | Full range >0 | The integration time, in units of 1/<math>f<sub>c</sub></math>.
`stand_i`      | `sint16` | [1-255]    | Stand number of the unconjugated stand.
`stand_j`      | `sint16` | [1-255]    | Stand number of the conjugated stand.
`freq_chan`    | `sint16` | [1-`NFREQCHAN`]   | Frequency channel id of the first channel in the packet.

Each packet payload shall contain 144 frequency channels (each
spanning a bandwidth of <math>f<sub>c</sub></math>) and 4 polarisation
products for one baseline (unique pair of stands). The data shall be
ordered with frequency channel changing slowest, followed by the
polarisation of the unconjugated stand, the polarisation of the
conjugated stand, and finally a packed value of 8 bytes, for a total
payload size of 4608 bytes.

    Slowest-changing                           Fastest-changing
    [144 chans][2 pol_i (X,Y)][2 pol_j (X,Y)][8 byte structure] = 4608 bytes

Polarisations are ordered X then Y, giving a combined order of XiXj,
XiYj, YiXj, YiYj, where the 'j' elements represent the conjugated
stand in the product. The 8-byte value structure shall contain the
real and imaginary components of a complex number, each 21 bits,
followed by a weight value with format `fixed22.21`. All three values
are signed and in two's complement format, with the structure packed
MSB first. Negative weight values indicate that samples were flagged.

    MSB                               LSB
    0        8        16       24     31
    ======== ======== ======== ========
    <------- --REAL-- ----><-- --------
    IMAG---- -><----- -WEIGHT- ------->
    ======== ======== ======== ========