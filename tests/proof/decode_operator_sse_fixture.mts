/** Decode a production SSE fixture with the actual, hash-pinned sibling UI decoder. */

import crypto from 'node:crypto'
import fs from 'node:fs'
import { pathToFileURL } from 'node:url'

const EXPECTED_DECODER_SHA256 = '888680f1f5515bc531fa841581f3a18e95e9043975c243405ae2335957a64585'

if (process.argv.length !== 4) {
  throw new Error('usage: decode_operator_sse_fixture.mts DECODER_SOURCE SSE_FIXTURE')
}
const decoderPath = process.argv[2]
const fixturePath = process.argv[3]
const decoderDigest = crypto.createHash('sha256').update(fs.readFileSync(decoderPath)).digest('hex')
if (decoderDigest !== EXPECTED_DECODER_SHA256) {
  throw new Error(`decoder SHA-256 ${decoderDigest} != pinned ${EXPECTED_DECODER_SHA256}`)
}

const { decodeOperatorEvent } = await import(pathToFileURL(decoderPath).href)
const frame = fs.readFileSync(fixturePath, 'utf8')
const eventName = /^event: (.+)$/m.exec(frame)?.[1]
const eventId = /^id: (.+)$/m.exec(frame)?.[1]
const data = /^data: (.+)$/m.exec(frame)?.[1]
if (eventName !== 'operator-event' || eventId === undefined || data === undefined) {
  throw new Error('invalid SSE envelope')
}
const decoded = decodeOperatorEvent(JSON.parse(data), {
  expectedHiveId: 'github/beadhive/beadhive',
})
if (!decoded.ok) throw new Error(decoded.refusal.message)
const expectedId = `${decoded.value.producerEpoch}:${decoded.value.sequence}`
if (eventId !== expectedId) throw new Error(`SSE id ${eventId} != ${expectedId}`)
if (decoded.value.baseSequence !== decoded.value.sequence - 1) {
  throw new Error('non-contiguous decoded event')
}
console.log(JSON.stringify({
  ok: true,
  event: eventName,
  idMatches: true,
  payloadKind: decoded.value.payload.kind,
  hiveId: decoded.value.hiveId,
  decoderSha256: decoderDigest,
}))
