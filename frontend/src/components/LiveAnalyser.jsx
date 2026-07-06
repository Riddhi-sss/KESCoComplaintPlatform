import { useState } from 'react'
import { api } from '../api'
import { UrgencyBadge } from './UrgencyBadge'

const QUEUE_LABELS = {
  bypass_emergency: { label: 'Bypass — Emergency', cls: 'badge-critical' },
  priority_queue:   { label: 'Priority queue',     cls: 'badge-high'     },
  standard_queue:   { label: 'Standard queue',     cls: 'badge-low'      },
}

const AREA_RISK_CLASS = {
  HIGH:   'badge-critical',
  MEDIUM: 'badge-high',
  LOW:    'badge-low',
}

export function LiveAnalyser({ uploadInfo }) {
  const [complaintNo, setComplaintNo] = useState('')
  const [loading, setLoading]         = useState(false)
  const [result, setResult]           = useState(null)
  const [error, setError]             = useState(null)

  const canLookup = !!(uploadInfo && uploadInfo.has_complaint_no)

  async function handleLookup() {
    if (!complaintNo.trim() || !uploadInfo) return
    setLoading(true)
    setResult(null)
    setError(null)
    try {
      const data = await api.lookupComplaint(uploadInfo.upload_id, complaintNo.trim())
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const queue = result ? QUEUE_LABELS[result.queue_routing] : null
  const geo = result?.general_reopen_rate_by_geo

  return (
    <div className="card">
      <h2 style={{ marginBottom: 8 }}>Live complaint analyser</h2>
      <p style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 16 }}>
        Look up a single complaint by its number from a file you've already uploaded, and get
        an instant full NLP breakdown — urgency, fault category, recommended action, and area
        reopen risk.
      </p>

      {!canLookup && (
        <div className="info-box" style={{ marginBottom: 16 }}>
          {uploadInfo
            ? `"${uploadInfo.filename}" has no complaint-number column, so lookup isn't available for it.`
            : 'Upload a file in the "Upload & analyse" tab to enable lookup by complaint number.'}
        </div>
      )}

      {canLookup && (
        <>
          <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 8 }}>
            Searching within: <strong>{uploadInfo.filename}</strong>
          </p>
          <input
            type="text"
            value={complaintNo}
            onChange={e => setComplaintNo(e.target.value)}
            placeholder="e.g. KS11042501136"
            style={{ marginBottom: 10, width: '100%' }}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <button className="btn-primary" onClick={handleLookup} disabled={loading || !complaintNo.trim()}>
              {loading ? <><span className="spinner" />Looking up…</> : 'Find & analyse'}
            </button>
            {error && <span style={{ fontSize: 13, color: 'var(--red)' }}>{error}</span>}
          </div>
        </>
      )}

      {result && (
        <div style={{ marginTop: 20, borderTop: '1px solid var(--border)', paddingTop: 16 }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 16 }}>
            {result.complaint_no && <span className="badge badge-gray">#{result.complaint_no}</span>}
            <UrgencyBadge level={result.urgency} />
            {queue && <span className={`badge ${queue.cls}`}>{queue.label}</span>}
            <span className="badge badge-info">{result.semantic_cluster}</span>
            <span className="badge badge-gray">{result.fault_category}</span>
            {result.area_reopen_risk && (
              <span className={`badge ${AREA_RISK_CLASS[result.area_reopen_risk] || 'badge-gray'}`}>
                Area reopen risk: {result.area_reopen_risk}
              </span>
            )}
          </div>

          <ResultTable
            rows={[
              ['Urgency rationale',   result.urgency_rationale],
              ['Recommended action',  result.recommended_action],
              ['Queue routing',       result.queue_routing?.replace(/_/g, ' ')],
              ['Location',           result.extracted_entities?.location || '—'],
              ['Infrastructure',     result.extracted_entities?.infrastructure || '—'],
              ['Duration mentioned', result.extracted_entities?.duration_mentioned || '—'],
              ['Affected services',  result.extracted_entities?.affected_services || '—'],
              ['Risk indicators',    result.risk_indicators?.join(', ') || 'None detected'],
              ['Confidence',         result.confidence != null ? `${(result.confidence * 100).toFixed(0)}%` : '—'],
              ['Existing category in file', result.existing_category_in_file || '—'],
              ['Substation reopen rate (general)',  geo?.substation  != null ? `${geo.substation}%`  : '—'],
              ['Subdivision reopen rate (general)', geo?.subdivision != null ? `${geo.subdivision}%` : '—'],
              ['Division reopen rate (general)',    geo?.division    != null ? `${geo.division}%`    : '—'],
            ]}
          />

          {geo && (
            <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 10, fontStyle: 'italic' }}>
              Reopen rates are general — they cover all complaint types from this location,
              not specifically the predicted fault category.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function ResultTable({ rows }) {
  return (
    <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label} style={{ borderTop: '1px solid var(--border)' }}>
            <td style={{ padding: '7px 0', color: 'var(--text-2)', width: 160, verticalAlign: 'top' }}>{label}</td>
            <td style={{ padding: '7px 0 7px 12px', color: 'var(--text)' }}>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}