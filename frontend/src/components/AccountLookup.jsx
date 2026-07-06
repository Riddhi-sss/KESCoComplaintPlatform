import { useState, useEffect, useCallback } from 'react'
import { api } from '../api'
import { UrgencyBadge } from './UrgencyBadge'

const PAGE_SIZE = 25

export function AccountLookup({ uploadInfo }) {
  const [search, setSearch]       = useState('')
  const [page, setPage]           = useState(0)
  const [listData, setListData]   = useState(null)
  const [listLoading, setListLoading] = useState(false)
  const [listError, setListError]     = useState(null)

  const [selectedAccount, setSelectedAccount] = useState(null)
  const [detail, setDetail]           = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError]     = useState(null)

  const canUse = !!(uploadInfo && uploadInfo.has_account_no)

  const loadAccounts = useCallback(() => {
    if (!canUse) return
    setListLoading(true); setListError(null)
    api.listAccounts(uploadInfo.upload_id, { search: search.trim() || undefined, limit: PAGE_SIZE, offset: page * PAGE_SIZE })
      .then(setListData)
      .catch(e => setListError(e.message))
      .finally(() => setListLoading(false))
  }, [canUse, uploadInfo, search, page])

  useEffect(() => { loadAccounts() }, [loadAccounts])

  function openAccount(accountNo) {
    setSelectedAccount(accountNo)
    setDetail(null); setDetailError(null); setDetailLoading(true)
    api.accountDetail(uploadInfo.upload_id, accountNo)
      .then(setDetail)
      .catch(e => setDetailError(e.message))
      .finally(() => setDetailLoading(false))
  }

  const totalPages = listData ? Math.max(1, Math.ceil(listData.total_accounts / PAGE_SIZE)) : 1

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card">
        <div className="section-header">
          <div className="section-header-icon" style={{ background: 'var(--blue-light)' }}>🔎</div>
          <div>
            <h2>Account-based complaint lookup</h2>
            <p style={{ fontSize: 13, color: 'var(--text-2)', marginTop: 2 }}>
              Pull every complaint tied to one consumer account from the uploaded file —
              useful for spotting repeat complainants and reviewing a consumer's full history.
            </p>
          </div>
        </div>

        {!uploadInfo && (
          <div className="info-box">
            Upload a file in the "Upload &amp; analyse" tab first.
          </div>
        )}

        {uploadInfo && !canUse && (
          <div className="info-box info-box-amber">
            "{uploadInfo.filename}" has no consumer account-number column, so account lookup
            isn't available for it. Download the upload template from the Upload tab to see the
            expected column name (ACCOUNT_NO).
          </div>
        )}

        {canUse && (
          <>
            <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
              <input
                type="text"
                value={search}
                onChange={e => { setSearch(e.target.value); setPage(0) }}
                placeholder="Search by account number…"
                style={{ maxWidth: 320 }}
              />
              {listData && (
                <span style={{ fontSize: 12, color: 'var(--text-3)', alignSelf: 'center' }}>
                  {listData.total_accounts.toLocaleString()} account{listData.total_accounts === 1 ? '' : 's'} found
                </span>
              )}
            </div>

            {listLoading && <p style={{ fontSize: 13, color: 'var(--text-3)' }}><span className="spinner spinner-dark" />Loading accounts…</p>}
            {listError && <p style={{ color: 'var(--red)', fontSize: 13 }}>{listError}</p>}

            {listData && !listLoading && (
              <>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 8, marginBottom: 14 }}>
                  {listData.accounts.map(a => (
                    <button
                      key={a.account_no}
                      onClick={() => openAccount(a.account_no)}
                      style={{
                        textAlign: 'left', padding: '10px 14px',
                        background: selectedAccount === a.account_no ? 'var(--blue-light)' : 'var(--surface)',
                        borderColor: selectedAccount === a.account_no ? 'var(--blue)' : 'var(--border)',
                      }}
                    >
                      <div style={{ fontSize: 13, fontWeight: 600, fontFamily: 'monospace' }}>{a.account_no}</div>
                      <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
                        {a.complaint_count} complaint{a.complaint_count === 1 ? '' : 's'}
                        {a.complaint_count > 1 && <span style={{ color: 'var(--amber)', fontWeight: 600 }}> · repeat</span>}
                      </div>
                    </button>
                  ))}
                  {listData.accounts.length === 0 && (
                    <p style={{ fontSize: 13, color: 'var(--text-3)' }}>No accounts match "{search}".</p>
                  )}
                </div>

                {totalPages > 1 && (
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <button disabled={page === 0} onClick={() => setPage(p => p - 1)} style={{ fontSize: 12 }}>← Prev</button>
                    <span style={{ fontSize: 12, color: 'var(--text-3)' }}>Page {page + 1} of {totalPages}</span>
                    <button disabled={page >= totalPages - 1} onClick={() => setPage(p => p + 1)} style={{ fontSize: 12 }}>Next →</button>
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>

      {detailLoading && (
        <div className="card"><p style={{ fontSize: 13, color: 'var(--text-3)' }}><span className="spinner spinner-dark" />Loading account history…</p></div>
      )}
      {detailError && (
        <div className="card"><p style={{ color: 'var(--red)', fontSize: 13 }}>{detailError}</p></div>
      )}

      {detail && !detailLoading && (
        <div className="card card-accent-blue">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 14, flexWrap: 'wrap', gap: 10 }}>
            <div>
              <h2 style={{ fontFamily: 'monospace' }}>Account {detail.account_no}</h2>
              <p style={{ fontSize: 13, color: 'var(--text-2)', marginTop: 4 }}>
                {detail.total_complaints} complaint{detail.total_complaints === 1 ? '' : 's'} on record in this file
                {detail.is_repeat_complainant && (
                  <span className="badge badge-high" style={{ marginLeft: 8 }}>Repeat complainant</span>
                )}
              </p>
            </div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {Object.entries(detail.urgency_breakdown).map(([lvl, n]) => (
                <span key={lvl} className={`badge badge-${lvl.toLowerCase()}`} style={{ fontSize: 11 }}>{lvl}: {n}</span>
              ))}
            </div>
          </div>

          <div className="divider" />

          <div>
            {detail.complaints.map((c, i) => (
              <div key={i} className={`complaint-row complaint-row-${c.urgency.toLowerCase()}`}>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6, flexWrap: 'wrap' }}>
                  <UrgencyBadge level={c.urgency} />
                  {c.date && <span className="badge badge-gray" style={{ fontSize: 11 }}>{c.date}</span>}
                  {c.complaint_no && (
                    <span style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'monospace' }}>#{c.complaint_no}</span>
                  )}
                  {c.substation && <span className="badge badge-gray" style={{ fontSize: 11 }}>📍 {c.substation}</span>}
                  {c.category && <span className="badge badge-info" style={{ fontSize: 11 }}>{c.category}</span>}
                  {c.status && <span className="badge badge-gray" style={{ fontSize: 11 }}>{c.status}</span>}
                </div>
                <p style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.5 }}>{c.text}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}