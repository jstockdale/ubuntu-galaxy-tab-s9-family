#!/usr/bin/env python3
"""Read one DISP-5 (panel cold boot) boot: what ran, what the DDIC answered,
and which DSI registers the bootloader left behind.

    python3 report.py <collect.sh directory | dmesg file>

Works on the tablet (no sudo needed for a directory collect.sh wrote; group
adm reads it) and on the host.  Input is a kernel ring buffer as `dmesg`
prints it; with msm.dsi_regdump=1 it holds one register dump of the DSI
controller and PHY at probe (as the bootloader left them) and one per enable
(just before the panel's first command).  The register comparison is
probe / first enable (the DDIC did not answer) / first enable after which it
answered.  A register is a bootloader leftover when the first enable still
holds the probe value and the working enable holds another one.
"""
import os
import re
import sys

LINE = re.compile(r"^(?:\[\s*(?P<t>\d+\.\d+)\]\s*)?(?P<msg>.*)$")
DUMP = re.compile(
    r"regdump (?P<when>\w+) (?P<block>ctrl|phy|lane|pll) \+(?P<off>[0-9a-f]+):"
    r" (?P<v0>[0-9a-f]{8}) (?P<v1>[0-9a-f]{8}) (?P<v2>[0-9a-f]{8}) (?P<v3>[0-9a-f]{8})")
PANEL_ID = re.compile(r"ana38407 panel id: ([0-9a-f]{2}) ([0-9a-f]{2}) ([0-9a-f]{2})")

# Physical offsets inside the DSI controller window (the dump starts at the
# block base, before the 6G register shift), from Samsung's dsi_ctrl_reg.h.
CTRL = {
    0x000: "HW_VERSION", 0x004: "CTRL", 0x008: "STATUS*", 0x00c: "FIFO_STATUS*",
    0x010: "VIDEO_MODE_CTRL", 0x03c: "COMMAND_MODE_DMA_CTRL",
    0x040: "COMMAND_MODE_MDP_CTRL", 0x044: "COMMAND_MODE_MDP_DCS_CMD_CTRL",
    0x048: "DMA_CMD_OFFSET", 0x04c: "DMA_CMD_LENGTH", 0x050: "DMA_FIFO_CTRL",
    0x054: "DMA_NULL_PACKET_DATA", 0x058: "CMD_MDP_STREAM0_CTRL",
    0x05c: "CMD_MDP_STREAM0_TOTAL", 0x060: "CMD_MDP_STREAM1_CTRL",
    0x064: "CMD_MDP_STREAM1_TOTAL", 0x068: "ACK_ERR_STATUS*",
    0x06c: "RDBK_DATA0*", 0x070: "RDBK_DATA1*", 0x074: "RDBK_DATA2*",
    0x078: "RDBK_DATA3*", 0x07c: "RDBK_DATATYPE0*", 0x080: "RDBK_DATATYPE1*",
    0x084: "TRIG_CTRL", 0x088: "EXT_MUX", 0x08c: "EXT_MUX_TE_PULSE_DETECT_CTRL",
    0x0a0: "MISR_CMD_CTRL", 0x0a4: "MISR_VIDEO_CTRL", 0x0a8: "LANE_STATUS*",
    0x0ac: "LANE_CTRL", 0x0b0: "LANE_SWAP_CTRL", 0x0b4: "DLN0_PHY_ERR*",
    0x0b8: "LP_TIMER_CTRL", 0x0bc: "HS_TIMER_CTRL", 0x0c0: "TIMEOUT_STATUS*",
    0x0c4: "CLKOUT_TIMING_CTRL", 0x0c8: "EOT_PACKET", 0x0cc: "EOT_PACKET_CTRL",
    0x0d0: "GENERIC_ESC_TX_TRIGGER", 0x10c: "ERR_INT_MASK0", 0x110: "INT_CTRL*",
    0x114: "IOBIST_CTRL", 0x118: "SOFT_RESET", 0x11c: "CLK_CTRL",
    0x120: "CLK_STATUS*", 0x124: "DEBUG_BUS_CTL", 0x128: "DEBUG_BUS_STATUS*",
    0x12c: "PHY_SW_RESET", 0x130: "AXI2AHB_CTRL", 0x144: "LANE_MISR_CTRL",
    0x15c: "TEST_PATTERN_GEN_CTRL", 0x180: "TEST_PATTERN_GEN_VIDEO_ENABLE",
    0x194: "COMMAND_MODE_MDP_IDLE_CTRL", 0x19c: "TPG_MAIN_CONTROL",
    0x1b8: "COMMAND_MODE_MDP_CTRL2", 0x1bc: "CMD_MDP_STREAM2_CTRL",
    0x1c0: "CMD_MDP_STREAM2_TOTAL", 0x1cc: "VBIF_CTRL", 0x1d0: "AES_CTRL",
    0x1d4: "RDBK_DATA_CTRL", 0x1e4: "DSI_TIMING_FLUSH", 0x1e8: "DSI_TIMING_DB_MODE",
    0x1f4: "VERSION", 0x200: "DYNAMIC_REFRESH_CTRL", 0x210: "DYNAMIC_REFRESH_STATUS*",
    0x2a0: "VIDEO_COMPRESSION_MODE_CTRL", 0x2a4: "VIDEO_COMPRESSION_MODE_CTRL2",
    0x2a8: "COMMAND_COMPRESSION_MODE_CTRL", 0x2ac: "COMMAND_COMPRESSION_MODE_CTRL2",
    0x2b0: "COMMAND_COMPRESSION_MODE_CTRL3", 0x2b4: "COMMAND_MODE_NULL_INSERTION_CTRL",
    0x2b8: "READ_BACK_DISABLE_STATUS*", 0x2bc: "DESKEW_CTRL", 0x2c0: "DESKEW_DELAY_CTRL",
    0x2c8: "DEBUG_CTRL", 0x2cc: "SECURE_DISPLAY_STATUS*", 0x2d8: "CPHY_MODE_CTRL",
    0x310: "LOGICAL_LANE_SWAP_CTRL", 0x330: "SPLIT_LINK",
}
# DSI PHY v5.2 common block (Samsung's dsi_phy_hw_v5_0.c).
PHY = {
    0x000: "REVISION_ID0", 0x010: "CLK_CFG0", 0x014: "CLK_CFG1", 0x018: "GLBL_CTRL",
    0x01c: "RBUF_CTRL", 0x020: "VREG_CTRL_0", 0x024: "CTRL_0", 0x028: "CTRL_1",
    0x02c: "CTRL_2", 0x030: "CTRL_3", 0x034: "LANE_CFG0", 0x038: "LANE_CFG1",
    0x03c: "PLL_CNTRL", 0x040: "DPHY_SOT", 0x0a0: "LANE_CTRL0",
    0x0a4: "LANE_CTRL1 (ULPS req 0-4, force clk HS 5-6)",
    0x0a8: "LANE_CTRL2 (ULPS exit)", 0x0ac: "LANE_CTRL3 (force stop)",
    0x0b0: "LANE_CTRL4", 0x0ec: "GLBL_HSTX_STR_CTRL_0", 0x0f0: "GLBL_HSTX_STR_CTRL_1",
    0x0f4: "GLBL_RESCODE_OFFSET_TOP", 0x0f8: "GLBL_RESCODE_OFFSET_BOT",
    0x0fc: "GLBL_RESCODE_OFFSET_MID", 0x100: "GLBL_LPTX_STR_CTRL",
    0x104: "GLBL_PEMPH_CTRL_0", 0x108: "GLBL_PEMPH_CTRL_1",
    0x10c: "GLBL_STR_SWI_CAL_SEL_CTRL", 0x110: "VREG_CTRL_1", 0x114: "CTRL_4",
    0x140: "PHY_STATUS*", 0x148: "LANE_STATUS0* (ULPS)", 0x14c: "LANE_STATUS1* (stop)",
    0x1ac: "GLBL_DIGTOP_SPARE10", 0x1b4: "SL_DSI_LANE_CTRL1",
}
for _i in range(14):
    PHY[0x0b4 + 4 * _i] = "TIMING_CTRL_%d" % _i
LANE_REGS = {0x00: "CFG0", 0x04: "CFG1", 0x08: "CFG2", 0x0c: "TEST_DATAPATH",
             0x10: "PIN_SWAP", 0x14: "LPRX_CTRL", 0x18: "TX_DCTRL"}


def reg_name(block, off):
    if block == "ctrl":
        return CTRL.get(off, "")
    if block == "phy":
        return PHY.get(off, "")
    if block == "lane":
        lane, reg = divmod(off, 0x80)
        name = LANE_REGS.get(reg)
        if name:
            return "%s_%s" % ("CLK" if lane == 4 else "LN%d" % lane, name)
        return ""
    return ""


def is_status(block, off):
    return reg_name(block, off).split(" ")[0].endswith("*")


def read_input(path):
    if os.path.isdir(path):
        dmesg = os.path.join(path, "dmesg.txt")
        cmdline_path = os.path.join(path, "cmdline.txt")
        cmdline = open(cmdline_path).read() if os.path.exists(cmdline_path) else ""
    else:
        dmesg, cmdline = path, ""
    lines = open(dmesg, errors="replace").read().splitlines()
    if not cmdline:
        for line in lines:
            if "Kernel command line:" in line:
                cmdline = line.split("Kernel command line:", 1)[1]
                break
    return lines, cmdline


def parse(lines):
    """Return (events, dumps).  A dump is one (when, block) pass from +000."""
    events, dumps = [], []
    current = {}
    for index, raw in enumerate(lines):
        m = LINE.match(raw)
        t = float(m.group("t")) if m.group("t") else None
        msg = m.group("msg")
        d = DUMP.search(msg)
        if d:
            key = (d.group("when"), d.group("block"))
            off = int(d.group("off"), 16)
            if off == 0 or key not in current:
                current[key] = {"when": key[0], "block": key[1], "t": t,
                                "index": index, "regs": {}}
                dumps.append(current[key])
            for k in range(4):
                current[key]["regs"][off + 4 * k] = int(d.group("v%d" % k), 16)
            continue
        events.append((index, t, msg))
    return events, dumps


INTERESTING = [
    ("tcon", re.compile(r"first power-on: .*|power-on \d+: .*")),
    ("id", PANEL_ID),
    ("dead", re.compile(r"panel id 00 00 00: the DDIC did not answer")),
    ("invalid", re.compile(r"msm_dsi_host_cmd_rx:Invalid response cmd")),
    ("handoff", re.compile(r"bootloader hand-off: .*")),
    ("cycle", re.compile(r"boot display cycle: .*")),
    ("suspend", re.compile(r"PM: suspend (entry|exit).*|PM: suspend debug: .*")),
    ("dphy", re.compile(r"D-PHY timings .*")),
    ("pll", re.compile(r"PLL turned on before configuring PHY|Turning OFF PHY while PLL is on")),
    ("error", re.compile(r".*(frame done time|kickoff time|pp done time|underrun|"
                         r"dsi_err|DSI error|cmd dma tx failed|Read cmd Tx failed|"
                         r"wait for (completion|vblank)|\[dpu error\]).*", re.I)),
    ("vrr", re.compile(r"VRR_SETTING .*")),
    ("gdm", re.compile(r"replaying deferred Type-C DP HPD.*")),
]


def timeline(events):
    rows, invalid = [], []
    for index, t, msg in events:
        for kind, rx in INTERESTING:
            m = rx.search(msg)
            if not m:
                continue
            if kind == "invalid":
                invalid.append(t)
            else:
                rows.append((index, t, kind, m.group(0)))
            break
    # The id and cell id reads of one power-on fail within a few ms of the
    # id line; attribute 'Invalid response' lines to the nearest id line.
    out = []
    for index, t, kind, text in rows:
        if kind == "id" and t is not None:
            near = sum(1 for x in invalid if x is not None and -0.5 <= x - t <= 0.2)
            if near:
                text += "   (%d x 'Invalid response cmd' around it)" % near
        out.append((index, t, kind, text))
    if invalid and all(t is None for t in invalid):
        out.append((len(events), None, "invalid", "%d x 'Invalid response cmd'" % len(invalid)))
    return out


def fmt_t(t):
    return "%9.3f" % t if t is not None else "        ?"


def enables(dumps, rows):
    """Group enable dumps and attach the first panel id that follows each."""
    groups = []
    for dump in dumps:
        if dump["when"] != "enable":
            continue
        if dump["block"] == "ctrl" or not groups:
            groups.append({"t": dump["t"], "index": dump["index"], "blocks": {}})
        groups[-1]["blocks"].setdefault(dump["block"], dump["regs"])
    ids = [(i, t, text) for i, t, kind, text in rows if kind == "id"]
    for n, group in enumerate(groups):
        end = groups[n + 1]["index"] if n + 1 < len(groups) else float("inf")
        found = [x for x in ids if group["index"] < x[0] < end]
        group["id"] = PANEL_ID.search(found[0][2]).groups() if found else None
    return groups


def probe_blocks(dumps):
    blocks = {}
    for dump in dumps:
        if dump["when"] == "probe":
            blocks.setdefault(dump["block"], dump["regs"])
    return blocks


def compare(probe, bad, good, out):
    """Print the registers that differ between the bad and the good enable."""
    leftovers = []
    for block in ("ctrl", "phy", "lane", "pll"):
        b, g = bad.get(block), good.get(block)
        if not b or not g:
            continue
        p = probe.get(block, {})
        rows = []
        for off in sorted(set(b) & set(g)):
            if b[off] == g[off]:
                continue
            pv = p.get(off)
            leftover = pv is not None and pv == b[off] and not is_status(block, off)
            rows.append((off, pv, b[off], g[off], leftover))
            if leftover:
                leftovers.append((block, off, pv, g[off]))
        out.append("-- %s: %d register(s) differ between the bad and the good enable"
                   % (block, len(rows)))
        if rows:
            out.append("   %-6s %-10s %-10s %-10s  %s" % ("off", "probe", "bad", "good", "name"))
        for off, pv, bv, gv, leftover in rows:
            out.append("   +%03x   %-10s %08x   %08x   %s%s" % (
                off, "%08x" % pv if pv is not None else "-", bv, gv,
                reg_name(block, off), "   <== bootloader leftover" if leftover else ""))
    return leftovers


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    lines, cmdline = read_input(sys.argv[1])
    events, dumps = parse(lines)
    rows = timeline(events)
    out = []
    switches = [tok for tok in cmdline.split()
                if tok.startswith(("msm.dsi_regdump", "msm.mdss_handoff", "msm.boot_display",
                                   "panel_samsung_ana38407.", "systemd.mask="))]
    masked = "systemd.mask=ubuntu-gts9u-panel-coldboot-recover.service" in cmdline
    out.append("== boot")
    out.append("switches: %s" % (" ".join(switches) or "(none)"))
    out.append("cold-boot cycle unit: %s" % ("MASKED for this boot" if masked else "normal"))
    out.append("== timeline")
    for _i, t, _kind, text in rows:
        out.append("%s  %s" % (fmt_t(t), text))

    id_rows = [(i, PANEL_ID.search(text).groups()) for i, _t, kind, text in rows if kind == "id"]
    ids = [answer for _i, answer in id_rows]
    suspended = any(kind == "suspend" and "entry" in text for _i, _t, kind, text in rows)
    handoff = [(i, text) for i, _t, kind, text in rows if kind == "handoff"]
    cycle = [(i, text) for i, _t, kind, text in rows if kind == "cycle"]

    def first_answer_after(index):
        return next((answer for i, answer in id_rows if i > index), None)
    errors = [text for _i, _t, kind, text in rows if kind == "error"]
    good_id = ("80", "00", "04")
    dead_id = ("00", "00", "00")

    out.append("== verdict")
    if not ids:
        out.append("no 'ana38407 panel id' line: the panel never powered on in this log")
    else:
        first = ids[0]
        out.append("first answer: %s; answers in order: %s" % (
            " ".join(first), ", ".join(" ".join(i) for i in ids)))
        # The switch must have done its job and the next power-on answered.
        if handoff:
            index, text = handoff[0]
            ok = "power-cycled" in text and first_answer_after(index) == good_id
            out.append("E1 (GDSC cycle at probe): %s - %s" % ("PASS" if ok else "FAIL", text))
        if cycle:
            index, text = cycle[0]
            ok = "power-cycled" in text and first_answer_after(index) == good_id
            out.append("E2 (display-only boot cycle): %s - %s" % ("PASS" if ok else "FAIL", text))
        if first == dead_id and good_id in ids and not handoff and not cycle:
            how = ("after a system suspend cycle (the boot-time pm_test cycle or a manual one)"
                   if suspended else "WITHOUT any system suspend (a blank/unblank: E3 PASS)")
            out.append("recovered %s" % how)
        elif first == dead_id and good_id not in ids:
            out.append("never recovered in this log")
    if errors:
        out.append("display errors: %d (first: %s)" % (len(errors), errors[0]))

    out.append("== registers")
    if not dumps:
        out.append("no regdump lines (boot without msm.dsi_regdump=1)")
    else:
        probe = probe_blocks(dumps)
        groups = enables(dumps, rows)
        out.append("probe dumps: %s; enable dumps: %d" % (
            ", ".join(sorted(probe)) or "none", len(groups)))
        for n, g in enumerate(groups, 1):
            out.append("enable %d at %s: blocks %s, answer %s" % (
                n, fmt_t(g["t"]).strip(), ",".join(sorted(g["blocks"])),
                " ".join(g["id"]) if g["id"] else "-"))
        bad = next((g for g in groups if g["id"] == dead_id), None)
        good = next((g for g in groups if g["id"] and g["id"] != dead_id), None)
        if bad and good:
            leftovers = compare(probe, bad["blocks"], good["blocks"], out)
            out.append("-- leftovers (probe == bad != good, status registers excluded): %d" % len(leftovers))
            for block, off, pv, gv in leftovers:
                out.append("   %s +%03x %s: bootloader %08x, working %08x" % (
                    block, off, reg_name(block, off), pv, gv))
        else:
            out.append("need one enable that read 00 00 00 and one that answered to compare")
        if probe.get("ctrl"):
            p = probe["ctrl"]
            out.append("-- bootloader state at probe: CTRL %08x LANE_CTRL %08x CLK_CTRL %08x"
                       " LP_TIMER %08x HS_TIMER %08x DYN_REFRESH %08x" % tuple(
                           p.get(o, 0) for o in (0x004, 0x0ac, 0x11c, 0x0b8, 0x0bc, 0x200)))
        if probe.get("phy"):
            p = probe["phy"]
            out.append("-- PHY at probe: CTRL_0 %08x LANE_CTRL0 %08x LANE_CTRL1 %08x"
                       " LANE_CTRL2 %08x LANE_CTRL3 %08x PLL_CNTRL %08x GLBL_CTRL %08x" % tuple(
                           p.get(o, 0) for o in (0x024, 0x0a0, 0x0a4, 0x0a8, 0x0ac, 0x03c, 0x018)))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
