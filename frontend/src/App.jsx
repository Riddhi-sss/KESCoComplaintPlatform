import { useState, useEffect } from 'react'
import { LiveAnalyser }   from './components/LiveAnalyser'
import { UploadAnalysis } from './components/UploadAnalysis'
import { AccountLookup }  from './components/AccountLookup'
import { api } from './api'

const TABS = [
  { id: 'upload',   label: '📁 Upload & analyse'   },
  { id: 'accounts', label: '🔎 Account lookup'      },
  { id: 'live',     label: '⟳ Live analyser'        },
]

const LOADING_METRICS = [
  { label: 'Substations tracked', value: '…', sub: 'Loading…' },
  { label: 'Fault categories',    value: '…', sub: 'Loading…' },
  { label: 'Semantic clusters',   value: '…', sub: 'Loading…' },
  { label: 'Critical keywords',   value: '…', sub: 'Loading…' },
]

export default function App() {
  const [tab, setTab] = useState('upload')
  const [metrics, setMetrics] = useState(LOADING_METRICS)

  // Set once a file finishes analysing in the Upload tab ({ upload_id,
  // filename, has_complaint_no, has_account_no }). Account Lookup and Live
  // Analyser both read this to offer "work on this upload" modes — neither
  // one re-uploads or re-parses anything, they just call endpoints scoped
  // to this upload_id.
  const [uploadInfo, setUploadInfo] = useState(null)

  useEffect(() => {
    api.getCategories()
      .then(data => {
        const substationCount = Object.keys(data.general_reopen_rate_by_substation || {}).length
        const clusterCount    = Object.keys(data.semantic_clusters || {}).length

        setMetrics([
          {
            label: 'Substations tracked',
            value: String(substationCount),
            sub: 'From supply_clean SUBSTATION',
          },
          {
            label: 'Fault categories',
            value: String((data.fault_categories || []).length),
            sub: 'Breakdown summary taxonomy',
          },
          {
            label: 'Semantic clusters',
            value: String(clusterCount),
            sub: 'NLP buckets',
          },
          {
            label: 'Critical keywords',
            value: String((data.critical_keywords || []).length),
            sub: 'Auto-bypass triggers',
          },
        ])
      })
      .catch(() => {
        // Backend unreachable — fall back to a clearly-marked unknown state
        // rather than silently showing stale or invented numbers.
        setMetrics(LOADING_METRICS.map(m => ({ ...m, value: '—', sub: 'Unavailable' })))
      })
  }, [])

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)', paddingBottom: 60 }}>
      <header style={{
        background: 'linear-gradient(135deg, #1a3a5c 0%, #1a63b0 100%)',
        borderBottom: '1px solid rgba(255,255,255,0.10)',
        padding: '14px 32px',
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        boxShadow: '0 2px 12px rgba(0,0,0,0.18)',
      }}>
        <div style={{
          width: 38, height: 38, borderRadius: 10,
          background: 'rgba(255,255,255,0.15)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 20, flexShrink: 0,
        }}>⚡</div>
        <div>
          <h1 style={{ fontSize: 17, fontWeight: 700, color: '#fff', letterSpacing: '-0.2px' }}>
            Complaint Analysis Platform
          </h1>
          <p style={{ fontSize: 12, color: 'rgba(255,255,255,0.60)', marginTop: 1 }}>
            KESCO — AI-powered complaint triage &amp; analysis
          </p>
        </div>
      </header>

      <main style={{ maxWidth: 980, margin: '0 auto', padding: '28px 24px' }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14, marginBottom: 28 }}>
          {metrics.map((m, i) => (
            <div key={m.label} className="metric-card" style={{
              '--accent': ['var(--blue)','var(--red)','var(--green)','var(--amber)'][i],
            }}>
              <div className="metric-label">{m.label}</div>
              <div className="metric-value">{m.value}</div>
              <div className="metric-sub">{m.sub}</div>
            </div>
          ))}
        </div>

        <div style={{
          display: 'flex', gap: 4, marginBottom: 22,
          borderBottom: '2px solid var(--border)',
        }}>
          {TABS.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                background: tab === t.id ? 'var(--blue-light)' : 'none',
                border: 'none',
                borderBottom: tab === t.id ? '2px solid var(--blue)' : '2px solid transparent',
                borderRadius: '8px 8px 0 0',
                padding: '10px 18px',
                fontSize: 14,
                fontWeight: tab === t.id ? 600 : 400,
                color: tab === t.id ? 'var(--blue)' : 'var(--text-2)',
                marginBottom: -2,
                transition: 'all 0.15s',
              }}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div style={{ display: tab === 'upload' ? 'block' : 'none' }}>
          <UploadAnalysis onUploadComplete={setUploadInfo} />
        </div>
        <div style={{ display: tab === 'accounts' ? 'block' : 'none' }}>
          <AccountLookup uploadInfo={uploadInfo} />
        </div>
        <div style={{ display: tab === 'live' ? 'block' : 'none' }}>
          <LiveAnalyser uploadInfo={uploadInfo} />
        </div>
      </main>
    </div>
  )
}