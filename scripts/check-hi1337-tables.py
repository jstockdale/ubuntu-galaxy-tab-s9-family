#!/usr/bin/env python3
"""Check kernel/drivers/hi1337_gts9u_tables.h against stock CamX sensor blobs.

    python3 -I scripts/check-hi1337-tables.py VENDOR/lib64/camera/com.samsung.sensormodule.*.bin

Samsung's sensormodule blobs (Parameter Parser V3) serialise each register
write as ten little-endian u32 words, [id][address][1][id+1][2][2][0][1][id+2]
[...], with the address inline; consecutive records form one regSetting table.
The registerData values follow each table in a pool de-duplicated against
everything pooled earlier in the file: each new value once, in order of first
use.

The script walks the tables in file order, keeping the set of pooled values.
A table's own pool is read as the words after its last record that are new
to that set.  A driver table matches a blob table when the addresses are the
same and replaying the de-duplication with the driver's values reproduces the
blob's pool word for word.  The values pooled before the first table are not
known; 0 and 1 are assumed, and the first table is also tried with 0 alone.
Read-only; prints one line per blob table.
"""
import pathlib
import re
import struct
import sys

HEADER = pathlib.Path(__file__).resolve().parents[1] / "kernel/drivers/hi1337_gts9u_tables.h"


def driver_tables():
    text = HEADER.read_text()
    tables = {}
    for m in re.finditer(r"static const struct hi1337_reg (\w+)\[\] = \{(.*?)\};", text, re.S):
        tables[m.group(1)] = [(int(a, 16), int(v, 16)) for a, v in
                              re.findall(r"\{ 0x([0-9a-f]+), 0x([0-9a-f]+) \}", m.group(2))]
    return tables


def blob_tables(blob):
    records = []
    i = 0
    while i + 40 <= len(blob):
        w = struct.unpack_from("<10I", blob, i)
        if (w[2] == 1 and w[3] == w[0] + 1 and w[4] in (1, 2) and w[5] in (1, 2)
                and w[7] == 1 and w[8] == w[0] + 2 and w[1] < 0x10000 and w[0] < 0x100000):
            records.append((i, w[1]))
            i += 40
            continue
        i += 1
    groups = []
    for record in records:
        if groups and record[0] == groups[-1][-1][0] + 40:
            groups[-1].append(record)
        else:
            groups.append([record])
    return groups


def pool_words(blob, group, count):
    start = group[-1][0] + 36    # the last record's tenth word is pool[0]
    return [struct.unpack_from("<I", blob, start + 4 * k)[0] for k in range(count)
            if start + 4 * k + 4 <= len(blob)]


def expected_pool(values, pooled):
    out, seen = [], set(pooled)
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def read_pool(blob, group, pooled):
    out = []
    for word in pool_words(blob, group, len(group)):
        if word in pooled or word in out:
            break
        out.append(word)
    return out


def main():
    tables = driver_tables()
    for path in sys.argv[1:]:
        blob = pathlib.Path(path).read_bytes()
        groups = blob_tables(blob)
        modes = re.findall(rb"\d\d\. (\d+x\d+)", blob)
        print("%s: %d register tables; modes %s" % (
            pathlib.Path(path).name, len(groups), ", ".join(m.decode() for m in modes)))
        seeds = ({0, 1}, {0})
        for seed in seeds:
            pooled = set(seed)
            lines = []
            first_ok = None
            for index, group in enumerate(groups):
                addresses = [a for _, a in group]
                # What this table pooled, read greedily: the new words after it
                # (the field that follows may add a stray word or two).
                actual = read_pool(blob, group, pooled)
                matches = []
                for name, regs in tables.items():
                    if [a for a, _ in regs] != addresses:
                        continue
                    want = expected_pool([v for _, v in regs], pooled)
                    # A table whose values were all pooled already cannot be
                    # told apart from any other such table: no verdict.
                    if want and actual[:len(want)] == want:
                        matches.append((name, want))
                if index == 0:
                    first_ok = bool(matches)
                lines.append("  table #%d @0x%x, %d writes, %d new pooled values: %s" % (
                    index, group[0][0], len(group), len(matches[0][1]) if matches else len(actual),
                    ("same values as " + ", ".join(n for n, _ in matches)) if matches
                    else "no driver table matches"))
                pooled |= set(matches[0][1] if matches else actual)
            if first_ok or seed is seeds[-1]:
                print("  (values pooled before the first table taken as %s)" % sorted(seed))
                print("\n".join(lines))
                break


if __name__ == "__main__":
    main()
