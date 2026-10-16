// node --test tests/js/  (tests/test_review_js.py runs this under pytest when node is installed)
// xLength has one right answer: X's own weighted count (twitter-text v3 config).
import assert from "node:assert/strict";
import { test } from "node:test";
import { compact, duration, signed, xAge, xLength } from "../../harness/review/static/js/lib/format.js";

test("Latin text counts one per character", () => {
  assert.equal(xLength("hello world"), 11);
  assert.equal(xLength(""), 0);
});

test("any URL counts as 23", () => {
  assert.equal(xLength("https://x.com/WNBA/status/2110824046833348936"), 23);
  assert.equal(xLength("see https://a.co"), 4 + 23);
  assert.equal(xLength("two https://a.com https://b.com"), 4 + 23 + 1 + 23);
});

test("a bare domain on a common TLD is a link too", () => {
  assert.equal(xLength("see espn.com"), 4 + 23);
  assert.equal(xLength("x.com/WNBA"), 23);
  assert.equal(xLength("(espn.com)"), 1 + 23 + 1);
});

test("trailing punctuation isn't part of a link", () => {
  assert.equal(xLength("espn.com/wnba/story."), 23 + 1);
  assert.equal(xLength("https://espn.com, then"), 23 + 6);
});

test("dotted words, numbers, handles and tags aren't links", () => {
  assert.equal(xLength("e.g. 3.14 Node.js U.S."), 22);
  assert.equal(xLength("mail@espn.com"), 13);
  assert.equal(xLength("#espn.com"), 9);
});

test("emoji count two, including joined sequences and flags", () => {
  assert.equal(xLength("🔥"), 2);
  assert.equal(xLength("👩🏽‍🏫"), 2);
  assert.equal(xLength("🇺🇸"), 2);
});

test("CJK counts two per character", () => {
  assert.equal(xLength("日本"), 4);
});

test("curly quotes and dashes in the light ranges count one", () => {
  assert.equal(xLength("“—”"), 3);
});

test("compact writes exact counts under ten thousand, abbreviates above, and a dash for none", () => {
  assert.equal(compact(2605), "2,605");
  assert.equal(compact(951935), "951.9K");
  assert.equal(compact(null), "–");
});

test("signed prefixes + or a true minus, and xAge floors to minutes, then hours", () => {
  assert.equal(signed(0.035), "+0.035");
  assert.equal(signed(-0.012), "−0.012");
  assert.equal(xAge(38), "38m");
  assert.equal(xAge(130), "2h");
});

test("duration rounds once, so a minute never shows 60 seconds", () => {
  assert.equal(duration(640), "640 ms");
  assert.equal(duration(999.6), "1.0 s");
  assert.equal(duration(4200), "4.2 s");
  assert.equal(duration(9960), "10 s");
  assert.equal(duration(119_600), "2 min 0 s");
  assert.equal(duration(125_000), "2 min 5 s");
  assert.equal(duration(null), "–");
});
