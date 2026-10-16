// node --test tests/js/  (tests/test_review_js.py runs this under pytest when node is installed)
// xLength has one right answer: X's own weighted count (twitter-text v3 config).
import assert from "node:assert/strict";
import { test } from "node:test";
import { compact, signed, xAge, xLength } from "../../harness/review/static/js/lib/format.js";

test("Latin text counts one per character", () => {
  assert.equal(xLength("hello world"), 11);
  assert.equal(xLength(""), 0);
});

test("any URL counts as 23", () => {
  assert.equal(xLength("https://x.com/WNBA/status/2110824046833348936"), 23);
  assert.equal(xLength("see https://a.co"), 4 + 23);
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

test("compact follows X: exact under ten thousand", () => {
  assert.equal(compact(2605), "2,605");
  assert.equal(compact(951935), "951.9K");
  assert.equal(compact(null), "–");
});

test("signed and xAge", () => {
  assert.equal(signed(0.035), "+0.035");
  assert.equal(signed(-0.012), "−0.012");
  assert.equal(xAge(38), "38m");
  assert.equal(xAge(130), "2h");
});
