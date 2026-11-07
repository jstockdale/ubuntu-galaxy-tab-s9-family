#!/usr/bin/env python3
"""Execute the driver's hall mapping and key filter with fake input sinks."""
from pathlib import Path
import subprocess
import sys
import tempfile

driver = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).resolve().parents[1] / "kernel/drivers/samsung_stm32_pogo.c")
source = driver.read_text(encoding="utf-8")


def function(name):
    start = source.index(f"static {name}")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


handlers = "\n".join(function(name) for name in (
    "bool samsung_pogo_key_withheld(",
    "void samsung_pogo_report_cover(",
    "void samsung_pogo_report_hall(",
))
shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
typedef uint8_t u8;
#define SW_LID 0
#define SW_TABLET_MODE 1
#define BTN_MISC 0x100
#define BTN_GEAR_UP 0x151
#define BTN_DPAD_UP 0x220
#define BTN_DPAD_RIGHT 0x223
#define POGO_HALL_NONE 0
#define POGO_HALL_CLOSED 1
#define POGO_HALL_NORMAL 2
#define POGO_HALL_BACK 3
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
struct input_dev { int sw[2]; int syncs; };
struct device { int warnings, infos; };
struct i2c_client { struct device dev; };
struct samsung_pogo { struct i2c_client *client; struct input_dev *input, *cover_switch;
    u8 hall_state; long hall_events; };
static void input_report_switch(struct input_dev *d, int code, int value) { d->sw[code] = !!value; }
static void input_sync(struct input_dev *d) { d->syncs++; }
static void atomic64_inc(long *v) { ++*v; }
#include <stdarg.h>
static void warn_fn(struct device *d, const char *fmt, ...) { (void)fmt; d->warnings++; }
static void info_fn(struct device *d, const char *fmt, ...) { (void)fmt; d->infos++; }
#define dev_warn_ratelimited warn_fn
#define dev_info info_fn
'''
tests = r'''
static void hall(struct samsung_pogo *p, const u8 *data, size_t len) { samsung_pogo_report_hall(p, data, len); }
int main(void) {
    struct i2c_client c = {0};
    struct input_dev kbd = {0}, sw = {0};
    struct samsung_pogo p = { .client = &c, .input = &kbd, .cover_switch = &sw };
    u8 one[1], two[2] = { 2, 2 };

    /* What probe and attach report before any hall packet. */
    sw.sw[SW_TABLET_MODE] = 1;
    p.hall_state = POGO_HALL_NORMAL; samsung_pogo_report_cover(&p);
    assert(!sw.sw[SW_LID] && !sw.sw[SW_TABLET_MODE]);

    one[0] = POGO_HALL_BACK; hall(&p, one, 1);      /* E_LID_360 */
    assert(!sw.sw[SW_LID] && sw.sw[SW_TABLET_MODE] && p.hall_state == POGO_HALL_BACK);
    one[0] = POGO_HALL_NORMAL; hall(&p, one, 1);
    assert(!sw.sw[SW_LID] && !sw.sw[SW_TABLET_MODE]);
    one[0] = POGO_HALL_CLOSED; hall(&p, one, 1);    /* E_LID_0 */
    assert(sw.sw[SW_LID] && !sw.sw[SW_TABLET_MODE]);
    /* Closed straight to folded back still opens the lid. */
    one[0] = POGO_HALL_BACK; hall(&p, one, 1);
    assert(!sw.sw[SW_LID] && sw.sw[SW_TABLET_MODE]);
    int infos = c.dev.infos, syncs = sw.syncs;
    hall(&p, one, 1);                                /* repeat: no report */
    assert(c.dev.infos == infos && sw.syncs == syncs);
    /* Malformed packets change nothing and are counted. */
    one[0] = 0; hall(&p, one, 1);
    one[0] = 4; hall(&p, one, 1);
    hall(&p, two, 2);
    hall(&p, two, 0);
    assert(c.dev.warnings == 4 && p.hall_state == POGO_HALL_BACK && sw.sw[SW_TABLET_MODE]);
    assert(p.hall_events == 9);
    /* Detach: a bare tablet, lid open. */
    p.hall_state = POGO_HALL_NONE; samsung_pogo_report_cover(&p);
    assert(!sw.sw[SW_LID] && sw.sw[SW_TABLET_MODE]);
    /* Without a keyboard input, hall packets are ignored entirely. */
    p.input = NULL; one[0] = POGO_HALL_CLOSED; hall(&p, one, 1);
    assert(!sw.sw[SW_LID] && p.hall_events == 9);

    /* SM-X910 profile: SW_LID on the keyboard itself, back-fold is open. */
    struct input_dev kbd2 = {0};
    struct samsung_pogo q = { .client = &c, .input = &kbd2, .hall_state = POGO_HALL_NORMAL };
    one[0] = POGO_HALL_CLOSED; hall(&q, one, 1); assert(kbd2.sw[SW_LID]);
    one[0] = POGO_HALL_BACK; hall(&q, one, 1);   assert(!kbd2.sw[SW_LID]);
    one[0] = POGO_HALL_NORMAL; hall(&q, one, 1); assert(!kbd2.sw[SW_LID]);
    assert(!kbd2.sw[SW_TABLET_MODE]);

    /* Key filter: classic button blocks and the D-pad only. */
    for (unsigned int code = 1; code <= 0x2ff; code++) {
        bool expected = (code >= 0x100 && code <= 0x151) || (code >= 0x220 && code <= 0x223);
        assert(samsung_pogo_key_withheld(code) == expected);
    }
    assert(!samsung_pogo_key_withheld(0x2c1) && !samsung_pogo_key_withheld(0x2f8));
    return 0;
}
'''
with tempfile.TemporaryDirectory() as directory:
    test = Path(directory) / "cover.c"
    binary = Path(directory) / "cover"
    test.write_text(shim + handlers + tests, encoding="utf-8")
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                    "-o", str(binary), str(test)], check=True)
    subprocess.run([str(binary)], check=True)
print("PASS hall mapping (closed, open, folded back, detach), malformed packets, both board profiles, key filter")
