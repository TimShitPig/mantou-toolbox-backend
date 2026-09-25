const fs = require('node:fs')
const path = require('node:path')
const { DatabaseSync } = require('node:sqlite')

function parseJson(value, fallback) {
  try {
    return value ? JSON.parse(value) : fallback
  } catch {
    return fallback
  }
}

function toUser(row) {
  if (!row) {
    return null
  }

  return {
    id: String(row.id),
    nickName: String(row.nick_name || ''),
    avatarUrl: String(row.avatar_url || ''),
  }
}

function toJob(row) {
  if (!row) {
    return null
  }

  return {
    id: row.id,
    ownerUserId: row.owner_user_id === null ? null : String(row.owner_user_id),
    source: row.source,
    link: row.link,
    book: parseJson(row.book_json, {}),
    status: row.status,
    total: Number(row.total || 0),
    completed: Number(row.completed || 0),
    error: row.error || '',
    manifest: parseJson(row.manifest_json, null),
    downloadPath: row.download_path || '',
    createdAt: Number(row.created_at || 0),
    updatedAt: Number(row.updated_at || 0),
  }
}

function createDatabase(databasePath) {
  fs.mkdirSync(path.dirname(databasePath), { recursive: true })
  const db = new DatabaseSync(databasePath)

  db.exec(`
    PRAGMA foreign_keys = ON;
    PRAGMA journal_mode = WAL;

    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      provider TEXT NOT NULL,
      provider_subject TEXT NOT NULL,
      nick_name TEXT NOT NULL DEFAULT '',
      avatar_url TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      UNIQUE(provider, provider_subject)
    );

    CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      expires_at INTEGER NOT NULL,
      revoked_at INTEGER,
      created_at INTEGER NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS download_jobs (
      id TEXT PRIMARY KEY,
      owner_user_id INTEGER,
      source TEXT NOT NULL,
      link TEXT NOT NULL,
      book_json TEXT NOT NULL,
      status TEXT NOT NULL,
      total INTEGER NOT NULL DEFAULT 1,
      completed INTEGER NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '',
      manifest_json TEXT,
      download_path TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS client_logs (
      id TEXT PRIMARY KEY,
      user_id INTEGER,
      level TEXT NOT NULL,
      scope TEXT NOT NULL,
      message TEXT NOT NULL,
      request_id TEXT NOT NULL,
      meta_json TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS system_logs (
      id TEXT PRIMARY KEY,
      level TEXT NOT NULL,
      source TEXT NOT NULL,
      message TEXT NOT NULL,
      request_id TEXT NOT NULL DEFAULT '',
      method TEXT NOT NULL DEFAULT '',
      path TEXT NOT NULL DEFAULT '',
      status_code INTEGER,
      meta_json TEXT NOT NULL DEFAULT '{}',
      created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS app_settings (
      key TEXT PRIMARY KEY,
      value_json TEXT NOT NULL,
      updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS admin_sessions (
      id TEXT PRIMARY KEY,
      expires_at INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      revoked_at INTEGER
    );

    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_download_jobs_owner ON download_jobs(owner_user_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_client_logs_created ON client_logs(created_at);
    CREATE INDEX IF NOT EXISTS idx_system_logs_created ON system_logs(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_system_logs_level_created ON system_logs(level, created_at DESC);
  `)

  const statements = {
    findUser: db.prepare('SELECT * FROM users WHERE provider = ? AND provider_subject = ?'),
    findUserById: db.prepare('SELECT * FROM users WHERE id = ?'),
    createUser: db.prepare(
      'INSERT INTO users (provider, provider_subject, nick_name, avatar_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)'
    ),
    updateUserOnLogin: db.prepare(
      `UPDATE users
       SET nick_name = CASE WHEN ? <> '' THEN ? ELSE nick_name END,
           avatar_url = CASE WHEN ? <> '' THEN ? ELSE avatar_url END,
           updated_at = ?
       WHERE id = ?`
    ),
    updateProfile: db.prepare(
      'UPDATE users SET nick_name = ?, avatar_url = ?, updated_at = ? WHERE id = ?'
    ),
    createSession: db.prepare(
      'INSERT INTO sessions (id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)'
    ),
    findSession: db.prepare(
      `SELECT sessions.id AS session_id, sessions.user_id, sessions.expires_at, sessions.revoked_at,
              users.id, users.nick_name, users.avatar_url
       FROM sessions JOIN users ON users.id = sessions.user_id
       WHERE sessions.id = ?`
    ),
    revokeSession: db.prepare('UPDATE sessions SET revoked_at = ? WHERE id = ?'),
    createJob: db.prepare(
      `INSERT INTO download_jobs
       (id, owner_user_id, source, link, book_json, status, total, completed, error, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, 'queued', 1, 0, '', ?, ?)`
    ),
    findJob: db.prepare('SELECT * FROM download_jobs WHERE id = ?'),
    markJobRunning: db.prepare(
      "UPDATE download_jobs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'"
    ),
    updateJobProgress: db.prepare(
      "UPDATE download_jobs SET total = ?, completed = ?, updated_at = ? WHERE id = ? AND status = 'running'"
    ),
    completeJob: db.prepare(
      `UPDATE download_jobs
       SET status = 'completed', completed = total, manifest_json = ?, download_path = ?, error = '', updated_at = ?
       WHERE id = ?`
    ),
    failJob: db.prepare(
      "UPDATE download_jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?"
    ),
    countCompletedJobsSince: db.prepare(
      "SELECT COUNT(*) AS count FROM download_jobs WHERE owner_user_id = ? AND status = 'completed' AND created_at >= ?"
    ),
    createClientLog: db.prepare(
      `INSERT INTO client_logs (id, user_id, level, scope, message, request_id, meta_json, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    ),
    createSystemLog: db.prepare(
      `INSERT INTO system_logs
       (id, level, source, message, request_id, method, path, status_code, meta_json, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ),
    pruneSystemLogsByAge: db.prepare('DELETE FROM system_logs WHERE created_at < ?'),
    pruneSystemLogsByCount: db.prepare(
      'DELETE FROM system_logs WHERE id IN (SELECT id FROM system_logs ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET 5000)'
    ),
    getSettings: db.prepare('SELECT key, value_json FROM app_settings'),
    setSetting: db.prepare(
      `INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, ?)
       ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at`
    ),
    createAdminSession: db.prepare(
      'INSERT INTO admin_sessions (id, expires_at, created_at) VALUES (?, ?, ?)'
    ),
    getAdminSession: db.prepare('SELECT * FROM admin_sessions WHERE id = ?'),
    revokeAdminSession: db.prepare('UPDATE admin_sessions SET revoked_at = ? WHERE id = ?'),
    adminMetrics: db.prepare(
      `SELECT
         (SELECT COUNT(*) FROM users) AS users,
         (SELECT COUNT(*) FROM download_jobs WHERE created_at >= ?) AS jobs_today,
         (SELECT COUNT(*) FROM download_jobs WHERE status IN ('queued', 'running')) AS jobs_active,
         (SELECT COUNT(*) FROM download_jobs WHERE status = 'completed' AND created_at >= ?) AS jobs_completed_today,
         (SELECT COUNT(*) FROM download_jobs WHERE status = 'failed' AND created_at >= ?) AS jobs_failed_today,
         (SELECT COUNT(*) FROM system_logs WHERE level = 'error' AND created_at >= ?) AS errors_24h`
    ),
    adminJobs: db.prepare('SELECT * FROM download_jobs ORDER BY created_at DESC LIMIT ?'),
    adminSystemLogs: db.prepare(
      `SELECT * FROM system_logs
       WHERE NOT (source = 'http' AND status_code = 404)
         AND NOT (source = 'http' AND status_code = 401 AND message LIKE '%admin_login_required%')
       ORDER BY created_at DESC, rowid DESC LIMIT ?`
    ),
  }

  function upsertUser(provider, subject, profile) {
    const now = Date.now()
    const normalizedProfile = profile || {}
    const nickName = String(normalizedProfile.nickName || '').trim().slice(0, 32)
    const avatarUrl = String(normalizedProfile.avatarUrl || '').trim().slice(0, 2048)
    const existing = statements.findUser.get(provider, subject)

    if (!existing) {
      const result = statements.createUser.run(
        provider,
        subject,
        nickName || '微信用户',
        avatarUrl,
        now,
        now
      )
      return toUser(statements.findUserById.get(Number(result.lastInsertRowid)))
    }

    statements.updateUserOnLogin.run(nickName, nickName, avatarUrl, avatarUrl, now, existing.id)
    return toUser(statements.findUserById.get(existing.id))
  }

  return {
    close() {
      db.close()
    },
    getUser(id) {
      return toUser(statements.findUserById.get(id))
    },
    upsertUser,
    updateUser(id, profile) {
      const current = statements.findUserById.get(id)
      if (!current) {
        return null
      }
      const nickName = String(profile.nickName ?? current.nick_name).trim().slice(0, 32)
      const avatarUrl = String(profile.avatarUrl ?? current.avatar_url).trim().slice(0, 2048)
      statements.updateProfile.run(nickName, avatarUrl, Date.now(), id)
      return toUser(statements.findUserById.get(id))
    },
    createSession(session) {
      statements.createSession.run(session.id, session.userId, session.expiresAt, session.createdAt)
    },
    getSession(id) {
      const row = statements.findSession.get(id)
      if (!row) {
        return null
      }
      return {
        id: row.session_id,
        userId: String(row.user_id),
        expiresAt: Number(row.expires_at),
        revokedAt: row.revoked_at === null ? null : Number(row.revoked_at),
        user: toUser(row),
      }
    },
    revokeSession(id) {
      statements.revokeSession.run(Date.now(), id)
    },
    createJob(job) {
      const now = Date.now()
      statements.createJob.run(
        job.id,
        job.ownerUserId === null || typeof job.ownerUserId === 'undefined' ? null : Number(job.ownerUserId),
        job.source,
        job.link,
        JSON.stringify(job.book || {}),
        now,
        now
      )
      return this.getJob(job.id)
    },
    getJob(id) {
      return toJob(statements.findJob.get(id))
    },
    markJobRunning(id) {
      statements.markJobRunning.run(Date.now(), id)
      return this.getJob(id)
    },
    updateJobProgress(id, total, completed) {
      const parsedTotal = Math.trunc(Number(total))
      const normalizedTotal = Number.isFinite(parsedTotal) ? Math.max(1, Math.min(parsedTotal, 1000000)) : 1
      const parsedCompleted = Math.trunc(Number(completed))
      const normalizedCompleted = Number.isFinite(parsedCompleted)
        ? Math.max(0, Math.min(normalizedTotal, parsedCompleted))
        : 0
      statements.updateJobProgress.run(normalizedTotal, normalizedCompleted, Date.now(), id)
      return this.getJob(id)
    },
    completeJob(id, manifest, downloadPath) {
      statements.completeJob.run(JSON.stringify(manifest), downloadPath, Date.now(), id)
      return this.getJob(id)
    },
    failJob(id, error) {
      statements.failJob.run(String(error || 'download_generation_failed').slice(0, 1000), Date.now(), id)
      return this.getJob(id)
    },
    countCompletedJobsSince(userId, startAt) {
      if (!userId) {
        return 0
      }
      const row = statements.countCompletedJobsSince.get(Number(userId), startAt)
      return Number(row && row.count || 0)
    },
    createClientLog(entry) {
      statements.createClientLog.run(
        entry.id,
        entry.userId ? Number(entry.userId) : null,
        entry.level,
        entry.scope,
        entry.message,
        entry.requestId,
        JSON.stringify(entry.meta || {}),
        entry.createdAt
      )
    },
    createSystemLog(entry) {
      statements.createSystemLog.run(
        entry.id,
        entry.level,
        entry.source,
        entry.message,
        entry.requestId || '',
        entry.method || '',
        entry.path || '',
        entry.statusCode ?? null,
        JSON.stringify(entry.meta || {}),
        entry.createdAt
      )
      statements.pruneSystemLogsByAge.run(Date.now() - 30 * 24 * 60 * 60 * 1000)
      statements.pruneSystemLogsByCount.run()
    },
    getAdminSettings() {
      const output = {}
      for (const row of statements.getSettings.all()) {
        output[row.key] = parseJson(row.value_json, null)
      }
      return output
    },
    setAdminSettings(settings) {
      const now = Date.now()
      db.exec('BEGIN')
      try {
        for (const [key, value] of Object.entries(settings)) {
          statements.setSetting.run(key, JSON.stringify(value), now)
        }
        db.exec('COMMIT')
      } catch (error) {
        db.exec('ROLLBACK')
        throw error
      }
      return this.getAdminSettings()
    },
    createAdminSession(session) {
      statements.createAdminSession.run(session.id, session.expiresAt, session.createdAt)
    },
    getAdminSession(id) {
      const row = statements.getAdminSession.get(id)
      if (!row) return null
      return {
        id: row.id,
        expiresAt: Number(row.expires_at),
        createdAt: Number(row.created_at),
        revokedAt: row.revoked_at === null ? null : Number(row.revoked_at),
      }
    },
    revokeAdminSession(id) {
      statements.revokeAdminSession.run(Date.now(), id)
    },
    getAdminSummary(since) {
      const metrics = statements.adminMetrics.get(since, since, since, Date.now() - 24 * 60 * 60 * 1000)
      const jobs = statements.adminJobs.all(12).map((row) => {
        const job = toJob(row)
        return {
          id: job.id,
          source: job.source,
          title: String(job.book && job.book.title || '未命名书籍'),
          status: job.status,
          error: job.error,
          createdAt: job.createdAt,
          updatedAt: job.updatedAt,
        }
      })
      const systemLogs = statements.adminSystemLogs.all(100).map((row) => ({
        id: row.id,
        level: row.level,
        source: row.source,
        message: row.message,
        requestId: row.request_id,
        method: row.method,
        path: row.path,
        statusCode: row.status_code === null ? null : Number(row.status_code),
        meta: parseJson(row.meta_json, {}),
        createdAt: Number(row.created_at || 0),
      }))
      return {
        metrics: Object.fromEntries(Object.entries(metrics).map(([key, value]) => [key, Number(value || 0)])),
        jobs,
        systemLogs,
      }
    },
  }
}

module.exports = {
  createDatabase,
}
