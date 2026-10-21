const fs = require("fs");

const asarPath = process.argv[2];
const wanted = process.argv.slice(3);

const fd = fs.openSync(asarPath, "r");
const sizeBuf = Buffer.alloc(8);
fs.readSync(fd, sizeBuf, 0, 8, 0);
const headerSize = sizeBuf.readUInt32LE(4);
const hbuf = Buffer.alloc(headerSize);
fs.readSync(fd, hbuf, 0, headerSize, 8);
const jsonLen = hbuf.readUInt32LE(4);
const header = JSON.parse(hbuf.toString("utf8", 8, 8 + jsonLen));
const baseOffset = 8 + headerSize;

const index = new Map();
function walk(node, prefix) {
  for (const name of Object.keys(node.files || {})) {
    const ent = node.files[name];
    const p = prefix ? prefix + "/" + name : name;
    if (ent.files) walk(ent, p);
    else index.set(p, ent);
  }
}
walk(header, "");

for (const w of wanted) {
  const ent = index.get(w);
  if (!ent) { console.log("=== MISSING " + w); continue; }
  if (ent.unpacked) {
    const t = fs.readFileSync(require("path").join(require("path").dirname(asarPath) + ".unpacked", w));
    console.log("=== " + w + " (unpacked, " + ent.size + " bytes)");
    console.log(t.toString("utf8"));
    continue;
  }
  const buf = Buffer.alloc(ent.size);
  fs.readSync(fd, buf, 0, ent.size, baseOffset + Number(ent.offset));
  console.log("=== " + w + " (" + ent.size + " bytes)");
  console.log(buf.toString("utf8"));
}
