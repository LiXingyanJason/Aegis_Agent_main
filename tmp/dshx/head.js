const fs = require("fs");

const asarPath = process.argv[2];
const target = process.argv[3];
const start = Number(process.argv[4] || 1);
const count = Number(process.argv[5] || 120);

const fd = fs.openSync(asarPath, "r");
const sizeBuf = Buffer.alloc(8);
fs.readSync(fd, sizeBuf, 0, 8, 0);
const headerSize = sizeBuf.readUInt32LE(4);
const hbuf = Buffer.alloc(headerSize);
fs.readSync(fd, hbuf, 0, headerSize, 8);
const jsonLen = hbuf.readUInt32LE(4);
const header = JSON.parse(hbuf.toString("utf8", 8, 8 + jsonLen));
const baseOffset = 8 + headerSize;

let ent = null;
(function walk(node, prefix) {
  for (const name of Object.keys(node.files || {})) {
    const e = node.files[name];
    const p = prefix ? prefix + "/" + name : name;
    if (e.files) walk(e, p);
    else if (p === target) ent = e;
  }
})(header, "");

if (!ent) { console.log("MISSING " + target); process.exit(1); }
const buf = Buffer.alloc(ent.size);
fs.readSync(fd, buf, 0, ent.size, baseOffset + Number(ent.offset));
const lines = buf.toString("utf8").split(/\r?\n/);
console.log(`### ${target} totalLines=${lines.length} size=${ent.size}`);
for (let i = start - 1; i < Math.min(start - 1 + count, lines.length); i++) {
  console.log(`${i + 1}\t${lines[i]}`);
}
