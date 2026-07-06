import { useState } from 'react'
import { api } from '../api'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, Legend,
} from 'recharts'

const URGENCY_COLORS = {
  CRITICAL: '#A32D2D',
  HIGH:     '#854F0B',
  MEDIUM:   '#185FA5',
  LOW:      '#3B6D11',
}

export function UploadAnalysis({ onUploadComplete }) {
  const [file, setFile]       = useState(null)
  const [loading, setLoading] = useState(false)
  const [progressLabel, setProgressLabel] = useState('')
  const [error, setError]     = useState(null)
  const [result, setResult]   = useState(null)

  const [insights, setInsights]             = useState({})
  const [insightLoading, setInsightLoading] = useState({})

  const [templateLoading, setTemplateLoading] = useState(false)
  const [reportLoading, setReportLoading]     = useState(false)
  const [reportError, setReportError]         = useState(null)

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  async function handleDownloadTemplate() {
    setTemplateLoading(true)
    try {
      const blob = await api.downloadTemplate()
      downloadBlob(blob, 'KESCO_complaint_upload_template.csv')
    } catch (e) {
      setError(e.message)
    } finally {
      setTemplateLoading(false)
    }
  }

  async function handleDownloadReport() {
    if (!result) return
    setReportLoading(true)
    setReportError(null)
    try {
      const blob = await api.downloadReport(result.upload_id)
      downloadBlob(blob, `KESCO_Operations_Report_${result.filename.replace(/\.[^/.]+$/, '')}.docx`)
    } catch (e) {
      setReportError(e.message)
    } finally {
      setReportLoading(false)
    }
  }

  async function handleUpload() {
    if (!file) return
    setLoading(true)
    setError(null)
    setResult(null)
    setInsights({})
    setProgressLabel('Uploading file…')
    try {
      const accepted = await api.uploadComplaints(file)
      setProgressLabel(`Analysing ${accepted.total_rows} rows… this can take a few minutes for large files`)

      // Poll until the background thread finishes. 600K-row files can take
      // several minutes, so this polls every 3s rather than hammering the
      // server, and just keeps going until status flips to "done" or "error".
      const uploadId = accepted.upload_id
      let status = 'processing'
      while (status === 'processing') {
        await new Promise(r => setTimeout(r, 3000))
        const statusRes = await api.uploadStatus(uploadId)
        status = statusRes.status
        if (status === 'error') {
          throw new Error(statusRes.error || 'Processing failed on the server.')
        }
      }

      setProgressLabel('Loading results…')
      const data = await api.uploadSummary(uploadId)
      setResult(data)
      if (onUploadComplete) {
        onUploadComplete({
          upload_id: data.upload_id,
          filename: data.filename,
          has_complaint_no: !!(data.columns_detected && data.columns_detected.complaint_no),
          has_account_no:   !!data.has_account_no,
        })
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setProgressLabel('')
    }
  }

  async function loadClusterInsight(clusterName) {
    if (!result || insights[clusterName]) return
    setInsightLoading(l => ({ ...l, [clusterName]: true }))
    try {
      const data = await api.uploadClusterInsight(result.upload_id, clusterName)
      setInsights(i => ({
        ...i,
        [clusterName]: { ...(data.ai_suggestions || {}), _note: data.ai_suggestions_note },
      }))
    } catch {
      setInsights(i => ({ ...i, [clusterName]: { insight: 'Failed to load insight.' } }))
    } finally {
      setInsightLoading(l => ({ ...l, [clusterName]: false }))
    }
  }

  const urgencyData = result
    ? Object.entries(result.urgency_breakdown).map(([level, count]) => ({ level, count }))
    : []

  const faultData = result
    ? Object.entries(result.fault_category_breakdown)
        .map(([name, count]) => ({ name, count }))
        .sort((a, b) => b.count - a.count)
    : []

  const substationData = result
    ? result.substation_summary.slice(0, 15).map(s => ({
        name: s.substation,
        critical_or_high: s.critical_or_high,
        total: s.total_complaints,
      }))
    : []

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, flexWrap: 'wrap' }}>
          <div>
            <h2 style={{ marginBottom: 8 }}>Upload complaint file</h2>
            <p style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 16, maxWidth: 560 }}>
              Upload a CSV or Excel export of complaints — a full month of data is fine, it's
              processed in the background. Urgency, fault category, substation hotspots,
              semantic clusters, MTTR, and account-level patterns are computed from whichever
              columns your file actually has.
            </p>
          </div>
          <button onClick={handleDownloadTemplate} disabled={templateLoading} style={{ fontSize: 13, flexShrink: 0 }}>
            {templateLoading ? <><span className="spinner spinner-dark" />Preparing…</> : '⬇ Download upload template'}
          </button>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            type="file"
            accept=".csv,.xlsx,.xls"
            onChange={e => setFile(e.target.files?.[0] || null)}
          />
          <button className="btn-primary" onClick={handleUpload} disabled={loading || !file}>
            {loading ? <><span className="spinner" />{progressLabel || 'Working…'}</> : 'Analyse file'}
          </button>
        </div>
        {loading && (
          <p style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 8 }}>
            Large files are processed in the background and may take a few minutes — this page will update automatically.
          </p>
        )}
        {error && <p style={{ color: 'var(--red)', fontSize: 13, marginTop: 10 }}>{error}</p>}
      </div>

      {result && (
        <>
          <div className="card card-accent-blue" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
            <div>
              <h2 style={{ marginBottom: 4 }}>📄 Operations report</h2>
              <p style={{ fontSize: 13, color: 'var(--text-2)' }}>
                Download a Word report with every chart and breakdown below, built for sharing
                with supervisors and decision-makers.
              </p>
              {reportError && <p style={{ color: 'var(--red)', fontSize: 13, marginTop: 6 }}>{reportError}</p>}
            </div>
            <button className="btn-primary" onClick={handleDownloadReport} disabled={reportLoading}>
              {reportLoading ? <><span className="spinner" />Building report…</> : '⬇ Download operations report (.docx)'}
            </button>
          </div>

          {result.urgency_note && (
            <div className="info-box" style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
              <span style={{ fontSize: 16, flexShrink: 0 }}>ℹ️</span>
              <span>{result.urgency_note}</span>
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14 }}>
            {[
              { label: 'Rows analysed', value: result.total_rows.toLocaleString(), sub: result.filename, color: 'var(--blue)', icon: '📄' },
              { label: 'Critical + High', value: ((result.urgency_breakdown.CRITICAL || 0) + (result.urgency_breakdown.HIGH || 0)).toLocaleString(), sub: 'Needs priority handling', color: 'var(--red)', icon: '🚨' },
              { label: 'Substations seen', value: result.substation_summary.length, sub: result.substation_summary_note ? 'No substation column' : 'Grouped by area', color: 'var(--green)', icon: '📍' },
              { label: 'Method', value: 'Keyword', sub: 'Embeddings deferred to triage filters', color: 'var(--amber)', icon: '⚙️' },
            ].map(m => (
              <div key={m.label} className="metric-card" style={{ borderTop: `3px solid ${m.color}` }}>
                <div style={{ fontSize: 20, marginBottom: 6 }}>{m.icon}</div>
                <div className="metric-label">{m.label}</div>
                <div className="metric-value" style={{ color: m.color, fontSize: 24 }}>{m.value}</div>
                <div className="metric-sub">{m.sub}</div>
              </div>
            ))}
          </div>

          <div className="card card-accent-blue">
            <div className="section-header">
              <div className="section-header-icon">🎯</div>
              <h2>Urgency breakdown</h2>
            </div>
            <ResponsiveContainer width="100%" height={260}>
              <PieChart>
                <Pie
                  data={urgencyData}
                  dataKey="count"
                  nameKey="level"
                  innerRadius={60}
                  outerRadius={95}
                  paddingAngle={2}
                >
                  {urgencyData.map(d => (
                    <Cell key={d.level} fill={URGENCY_COLORS[d.level] || '#888'} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          </div>

          <div className="card card-accent-red">
            <div className="section-header">
              <div className="section-header-icon" style={{ background: 'var(--red-light)' }}>🔧</div>
              <h2>Fault category breakdown</h2>
            </div>
            {result.fault_category_note ? (
              <p style={{ fontSize: 13, color: 'var(--text-3)', fontStyle: 'italic' }}>
                {result.fault_category_note}
              </p>
            ) : (
              <>
                <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>
                  Source column: {result.fault_category_source}
                </p>
                <ResponsiveContainer width="100%" height={Math.max(220, faultData.length * 28)}>
                  <BarChart data={faultData} layout="vertical" margin={{ left: 140 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                    <XAxis type="number" />
                    <YAxis type="category" dataKey="name" width={180} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Bar dataKey="count" fill="#185FA5" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </>
            )}
          </div>

          <div className="card card-accent-green">
            <div className="section-header">
              <div className="section-header-icon" style={{ background: 'var(--green-light)' }}>📍</div>
              <h2>Substation hotspots</h2>
            </div>
            {result.substation_summary_note ? (
              <p style={{ fontSize: 13, color: 'var(--text-3)', fontStyle: 'italic' }}>
                {result.substation_summary_note}
              </p>
            ) : (
              <>
                <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>
                  Top {substationData.length} substations by critical + high complaint count
                  (of {result.substation_summary.length} total seen in this file).
                </p>
                <ResponsiveContainer width="100%" height={Math.max(220, substationData.length * 26)}>
                  <BarChart data={substationData} layout="vertical" margin={{ left: 120 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                    <XAxis type="number" />
                    <YAxis type="category" dataKey="name" width={160} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Bar dataKey="critical_or_high" name="Critical + High" fill="#A32D2D" radius={[0, 4, 4, 0]} />
                    <Bar dataKey="total" name="Total" fill="#E6F1FB" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </>
            )}
          </div>

          <div className="card card-accent-amber">
            <div className="section-header">
              <div className="section-header-icon" style={{ background: 'var(--amber-light)' }}>🧩</div>
              <h2>Semantic clusters</h2>
            </div>
            <p style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 20 }}>
              {result.semantic_cluster_note ||
                'Five operational buckets computed from this file\u2019s own category data. Expand a cluster for an AI-generated operational insight based on its real sample complaints.'}
            </p>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12 }}>
              {result.semantic_cluster_summary.map(c => (
                <ClusterCard
                  key={c.cluster_name}
                  cluster={c}
                  insight={insights[c.cluster_name]}
                  isLoading={insightLoading[c.cluster_name]}
                  onLoadInsight={() => loadClusterInsight(c.cluster_name)}
                />
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function ClusterCard({ cluster, insight, isLoading, onLoadInsight }) {
  const c = cluster
  return (
    <div style={{
      background: 'var(--surface-2)',
      borderRadius: 'var(--radius-md)',
      padding: 16,
      border: '1px solid var(--border)',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 }}>
        <h3 style={{ fontSize: 13 }}>{c.cluster_name}</h3>
        <span className="badge badge-gray">{c.count} complaints</span>
      </div>

      <div style={{ fontSize: 24, fontWeight: 600, marginBottom: 6 }}>{c.share_pct}%</div>

      <div style={{ height: 4, borderRadius: 2, background: 'var(--border)', marginBottom: 12 }}>
        <div style={{ height: 4, borderRadius: 2, width: `${c.share_pct}%`, background: '#185FA5' }} />
      </div>

      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
        {Object.entries(c.urgency_breakdown).map(([lvl, n]) => (
          <span key={lvl} className="badge badge-gray" style={{ fontSize: 10 }}>{lvl}: {n}</span>
        ))}
      </div>

      <div style={{ marginBottom: 12 }}>
        {c.sample_complaints.map((s, i) => (
          <div key={i} style={{
            fontSize: 12,
            color: 'var(--text-2)',
            padding: '4px 0',
            borderBottom: i < c.sample_complaints.length - 1 ? '1px solid var(--border)' : 'none',
            lineHeight: 1.4,
          }}>
            "{s.length > 110 ? s.slice(0, 110) + '\u2026' : s}"
          </div>
        ))}
      </div>

      {!insight && (
        <button onClick={onLoadInsight} disabled={isLoading} style={{ fontSize: 12, padding: '5px 12px' }}>
          {isLoading ? <><span className="spinner" />Loading…</> : 'Load AI insight'}
        </button>
      )}

      {insight && (
        <div style={{ marginTop: 8, padding: 10, background: 'var(--surface)', borderRadius: 'var(--radius-sm)', border: '1px solid var(--border)' }}>
          <p style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6, marginBottom: insight.peak_risk_times ? 8 : 0 }}>
            {insight.insight}
          </p>
          {insight.peak_risk_times && (
            <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
              <strong>Peak risk:</strong> {insight.peak_risk_times}
            </p>
          )}
          {insight.suggested_sop && (
            <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
              <strong>SOP tip:</strong> {insight.suggested_sop}
            </p>
          )}
          {insight._note && (
            <p style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 8, fontStyle: 'italic', opacity: 0.75 }}>
              {insight._note}
            </p>
          )}
        </div>
      )}
    </div>
  )
}