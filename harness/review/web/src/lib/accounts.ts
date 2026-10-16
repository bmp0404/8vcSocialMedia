// The brand accounts the queue's drafts are written for, read once each from GET /api/account/<handle>.

import { useEffect, useState } from 'react'
import { api } from './api'
import type { Account } from './tweet'

const loaded = new Map<string, Promise<Account>>()

export function useAccounts(handles: (string | null)[]): Map<string, Account> {
  const [accounts, setAccounts] = useState(new Map<string, Account>())
  const key = [...new Set(handles.filter((h): h is string => !!h))].sort().join(',')
  useEffect(() => {
    let live = true
    for (const handle of key ? key.split(',') : []) {
      if (!loaded.has(handle)) loaded.set(handle, api.account(handle))
      loaded.get(handle)!.then(
        (a) => live && setAccounts((m) => (m.get(handle) === a ? m : new Map(m).set(handle, a))),
        () => loaded.delete(handle), // the draft keeps its initial avatar; the next render retries
      )
    }
    return () => {
      live = false
    }
  }, [key])
  return accounts
}
