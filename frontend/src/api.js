const BASE = import.meta.env.VITE_API_BASE || '/api'

async function post(path, body) {
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Request failed')
  }
  return res.json()
}

async function postForm(path, formData) {
  // No Content-Type header here on purpose — the browser sets the correct
  // multipart boundary itself when given a FormData body.
  const res = await fetch(BASE + path, { method: 'POST', body: formData })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Request failed')
  }
  return res.json()
}

async function get(path) {
  const res = await fetch(BASE + path)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Request failed')
  }
  return res.json()
}

export const api = {
  analyse: (text, account_no, substation, subdivision, division) =>
    post('/analyse', { text, account_no, substation, subdivision, division }),

  clusterInsight: (cluster_name, share_pct, sample_complaints) =>
    post('/cluster-insight', { cluster_name, share_pct, sample_complaints }),

  // Upload a CSV/XLSX File object. Returns immediately with
  // { upload_id, filename, total_rows, status: "processing" } — the heavy
  // analysis runs in a background thread on the server. Use uploadStatus()
  // to poll, then uploadSummary() once status is "done".
  uploadComplaints: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return postForm('/upload-complaints', fd)
  },

  // Poll this after uploadComplaints(). Returns { upload_id, status, error }
  // where status is "processing" | "done" | "error".
  uploadStatus: (uploadId) =>
    get(`/upload-complaints/${uploadId}/status`),

  // Call once status === "done". Returns the full analysis: urgency_breakdown,
  // fault_category_breakdown, substation_summary, semantic_cluster_summary, etc.
  uploadSummary: (uploadId) =>
    get(`/upload-complaints/${uploadId}/summary`),

  // AI insight for one cluster from a specific upload — pulls real
  // share_pct + sample complaints from that upload server-side, so the
  // frontend doesn't need to supply them.
  uploadClusterInsight: (uploadId, clusterName) =>
    get(`/upload-complaints/${uploadId}/cluster-insight?cluster_name=${encodeURIComponent(clusterName)}`),

  // Full /analyse-equivalent result for one complaint number, found within
  // a specific upload's rows.
  lookupComplaint: (uploadId, complaintNo) =>
    get(`/upload-complaints/${uploadId}/lookup?complaint_no=${encodeURIComponent(complaintNo)}`),

  // Paginated list of consumer accounts for an upload, most-complaints
  // first. `search` filters by account-number substring.
  listAccounts: (uploadId, { search, limit = 50, offset = 0 } = {}) => {
    const params = new URLSearchParams()
    if (search) params.set('search', search)
    params.set('limit', limit)
    params.set('offset', offset)
    return get(`/upload-complaints/${uploadId}/accounts?${params.toString()}`)
  },

  // Full complaint history + urgency/category breakdown for one account.
  accountDetail: (uploadId, accountNo) =>
    get(`/upload-complaints/${uploadId}/account/${encodeURIComponent(accountNo)}`),

  // Downloads a generated .docx operations report for this upload. Returns
  // a Blob — caller is responsible for turning it into a download link.
  downloadReport: async (uploadId) => {
    const res = await fetch(`${BASE}/upload-complaints/${uploadId}/report`)
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail || 'Report generation failed')
    }
    return res.blob()
  },

  // Downloads the blank CSV upload template showing every column name the
  // platform can detect.
  downloadTemplate: async () => {
    const res = await fetch(`${BASE}/template/download`)
    if (!res.ok) throw new Error('Could not download template')
    return res.blob()
  },

  // Returns { classification, ai_suggestions, ai_suggestions_note,
  //           general_reopen_rate_by_geo, data_scope_note }
  reopenRisk: (text, account_no, substation, subdivision, division) =>
    post('/reopen-risk', { text, account_no, substation, subdivision, division }),

  // Returns general (geography-based) reopen analytics — see
  // data_scope_note in the response for why there's no fault-type breakdown.
  reopenPatterns: () => get('/reopen-patterns'),

  getCategories: () => get('/categories'),

  stats: () => get('/stats'),

  health: () => get('/health'),
}