// ---- taskDataReadiness gate for the SCInsight Supervisor (Warehouse branch)
// Generated into a COPY of the Supervisor Agentflow by integrations/supplychain/build_readiness_supervisor.py.
// It runs after the Supervisor classified the request as "warehouse" and before the Warehouse specialist runs.
//
// FAIL CLOSED: not configured, unreachable, timeout, HTTP error, or any decision other than READY /
// READY_WITH_WARNINGS -> tdr_ready = "no" and the flow stops at "Stop · data not ready".
// This is a PREFLIGHT, not access control: the SCInsight API still authenticates and scopes every data call.
// The decision is made here, in code, not by asking the LLM to "check readiness" in a prompt.
const fetch = require('node-fetch')
const BASE = String($vars.tdr_api_url || '').replace(/\/+$/, '')
// DEMO ONLY: the name of a synthetic identity. NOT authentication. A real integration must pass a verified user
// identity (e.g. a token exchanged on the user's behalf); SCInsight flows currently only carry the shared agent key.
const DEMO_USER = String($vars.tdr_demo_user || '')
const out = (ready, message, id) => JSON.stringify({ tdr_ready: ready, tdr_message: message, tdr_evaluation_id: id || '' })
if (!BASE) return out('no', 'The data readiness check is not configured (Flowise variable tdr_api_url), so the warehouse analysis was not run.')

// ---- task context from the question (simple rules; the readiness service validates every value)
const q = String($question || '').toUpperCase()
const wh = (m) => (m ? (m[1] ? 'WH-' + m[1] : m[2]) : '')
const origin = wh(q.match(/\bFROM\s+(?:THE\s+)?(?:WAREHOUSE\s+([A-Z0-9])\b|(WH-[A-Z0-9]+)\b)/))
const destination = wh(q.match(/\b(?:TO|INTO)\s+(?:THE\s+)?(?:WAREHOUSE\s+([A-Z0-9])\b|(WH-[A-Z0-9]+)\b)/))
const skuWord = q.match(/\bSKU[\s#-]*([A-Z0-9]+(?:-[A-Z0-9]+)*)\b/)
const code = q.match(/\b(?!WH-)([A-Z]{2,}-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b/)
const product = skuWord ? 'SKU-' + skuWord[1] : (code ? code[1] : '')
const explicit = q.match(/(\d{4}-\d{2}-\d{2})\s*(?:\/|TO|UNTIL)\s*(\d{4}-\d{2}-\d{2})/)
// Integration default (documented): 14 days starting at the SAP snapshot date when the question names no period.
const asOf = String(($flow && $flow.state && $flow.state.as_of) || '')
let period = explicit ? explicit[1] + '/' + explicit[2] : ''
if (!period && /^\d{4}-\d{2}-\d{2}$/.test(asOf)) {
    const end = new Date(Date.parse(asOf + 'T00:00:00Z') + 13 * 86400000).toISOString().slice(0, 10)
    period = asOf + '/' + end
}
const context = {}
if (product) context.product_id = product
if (origin) context.origin_warehouse = origin
if (destination) context.destination_warehouse = destination
if (period) context.planning_period = period
const body = { task_id: 'warehouse_transfer', context }
if (DEMO_USER) body.demo_identity = { user: DEMO_USER, agent: 'scinsight_supervisor' }

// ---- call the readiness service
let res, data
try {
    res = await fetch(BASE + '/readiness/check', {
        method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body), timeout: 5000,
    })
    data = await res.json()
} catch (e) {
    return out('no', 'The data readiness service is unavailable (' + String((e && e.message) || e).slice(0, 120) + '), so the warehouse analysis was not run (fail closed).')
}
const decision = data && data.decision
const id = (data && data.evaluation_id) || ''
const reasons = (data && Array.isArray(data.reasons) ? data.reasons : []).slice(0, 6)
if (res.ok && (decision === 'READY' || decision === 'READY_WITH_WARNINGS')) {
    return out('yes', decision === 'READY' ? '' : 'Data readiness warnings: ' + reasons.join(' | '), id)
}
const why = reasons.length ? reasons.join('\n- ') : ((data && data.error && data.error.message) || ('HTTP ' + (res && res.status)))
return out('no', 'The warehouse analysis was not run because the data is not ready (evaluation ' + (id || 'n/a') + '):\n- ' + why, id)
