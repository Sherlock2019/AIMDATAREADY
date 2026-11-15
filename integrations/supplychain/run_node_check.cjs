// Run the readiness gate's JavaScript OUTSIDE Flowise, the way a Flowise Custom Function would run it.
// This tests the node's code against a running taskDataReadiness API; it does not test Flowise itself.
//
//   node integrations/supplychain/run_node_check.cjs <flow.json> <tdr_api_url> <demo_user|-> "<question>" [as_of]
//
// Prints the JSON string the node returns ({"tdr_ready": "yes"|"no", "tdr_message": ..., "tdr_evaluation_id": ...}).
const fs = require('fs')
const [flowPath, url, user, question, asOf] = process.argv.slice(2)
const flow = JSON.parse(fs.readFileSync(flowPath, 'utf8'))
const node = flow.nodes.find((n) => n.id === 'customFunctionAgentflow_tdr')
if (!node) { console.error('readiness gate node not found'); process.exit(2) }
const code = node.data.inputs.customFunctionJavascriptFunction
// Flowise bundles node-fetch v2 (which accepts a "timeout" option); emulate it with Node's built-in fetch.
const nodeFetch = (u, opts = {}) => {
    const { timeout, ...rest } = opts
    return fetch(u, { ...rest, signal: timeout ? AbortSignal.timeout(timeout) : undefined })
}
const shimRequire = (m) => (m === 'node-fetch' ? nodeFetch : require(m))
const run = new Function('require', '$vars', '$flow', '$question', `return (async () => {\n${code}\n})()`)
const vars = { tdr_api_url: url, tdr_demo_user: user === '-' ? '' : user }
run(shimRequire, vars, { state: { as_of: asOf || '' } }, question)
    .then((r) => process.stdout.write(r + '\n'))
    .catch((e) => { console.error(e); process.exit(2) })
