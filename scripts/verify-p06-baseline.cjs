// Read-only verification of the recorded P06 source and optional local artifact.
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const assert = require('node:assert/strict')
const root = path.resolve(__dirname, '..')
const evidence = require('../docs/audits/evidence/p06-baseline/artifact-binding.json')
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex')
const canonical = bytes => bytes.includes(0) ? bytes : Buffer.from(bytes.toString('utf8').replace(/\r\n/g, '\n'))
for (const source of evidence.sources) {
  assert.equal(sha(canonical(fs.readFileSync(path.join(root, source.path)))), source.canonicalSha256, source.path)
}
const release = process.argv[2] && path.resolve(process.argv[2])
if (release) {
  for (const artifact of evidence.artifacts) {
    assert.equal(sha(fs.readFileSync(path.join(release, artifact.path))), artifact.sha256, artifact.path)
  }
  const asar = require(path.join(root, 'desktop/node_modules/@electron/asar'))
  const archive = path.join(release, 'win-unpacked/resources/app.asar')
  let count = 0
  function compare(dir) {
    for (const item of fs.readdirSync(dir, {withFileTypes:true})) {
      const file = path.join(dir, item.name)
      if (item.isDirectory()) compare(file)
      else {
        const relative = path.relative(path.join(root, 'desktop'), file)
        assert.equal(sha(asar.extractFile(archive, relative)), sha(fs.readFileSync(file)), relative)
        count++
      }
    }
  }
  compare(path.join(root, 'desktop/dist'))
  assert.equal(count, evidence.asarDistFilesMatched)
  assert.equal(sha(fs.readFileSync(path.join(root, 'desktop/build/dist/smart-assistant-backend/smart-assistant-backend.exe'))), evidence.artifacts.find(a => a.path.endsWith('smart-assistant-backend.exe')).sha256)
}
console.log(JSON.stringify({sourcesMatched:evidence.sources.length, artifact:release ? 'MATCHED_RECORDED_ARTIFACT' : 'NOT_CHECKED', runtime:'NOT_RUN_BY_THIS_VERIFIER'}))
