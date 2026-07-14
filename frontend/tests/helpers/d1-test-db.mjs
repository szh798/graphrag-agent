import { DatabaseSync } from 'node:sqlite'

class TestD1Statement {
  constructor(database, sql, bindings = []) {
    this.database = database
    this.sql = sql
    this.bindings = bindings
  }

  bind(...bindings) {
    return new TestD1Statement(this.database, this.sql, bindings)
  }

  _prepared() {
    return this.database.prepare(this.sql)
  }

  _run() {
    const result = this._prepared().run(...this.bindings)
    return {
      success: true,
      meta: { changes: Number(result.changes) },
      results: [],
    }
  }

  async run() {
    return this._run()
  }

  async first(column) {
    const row = this._prepared().get(...this.bindings) ?? null
    return column && row ? row[column] ?? null : row
  }

  async all() {
    return {
      success: true,
      results: this._prepared().all(...this.bindings),
    }
  }
}

export class TestD1Database {
  constructor() {
    this.database = new DatabaseSync(':memory:')
    this.database.exec('PRAGMA foreign_keys = ON')
  }

  prepare(sql) {
    return new TestD1Statement(this.database, sql)
  }

  async batch(statements) {
    this.database.exec('BEGIN IMMEDIATE')
    try {
      const results = statements.map(statement => statement._run())
      this.database.exec('COMMIT')
      return results
    } catch (error) {
      this.database.exec('ROLLBACK')
      throw error
    }
  }

  rows(sql, ...bindings) {
    return this.database.prepare(sql).all(...bindings)
  }

  run(sql, ...bindings) {
    return this.database.prepare(sql).run(...bindings)
  }

  close() {
    this.database.close()
  }
}
