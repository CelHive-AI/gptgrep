import { execFile } from 'node:child_process'

/** Invoke the native binary without a shell and return its JSON object. */
export function runNative(args, binary = process.env.GPTGREP_BIN || 'gptgrep', timeout = 5 * 60 * 1000) {
  return new Promise((resolve, reject) => {
    const child = execFile(binary, args, {
      encoding: 'utf8',
      maxBuffer: 8 * 1024 * 1024,
      timeout,
      windowsHide: true,
    }, (error, stdout, stderr) => {
      process.removeListener('SIGINT', interrupt)
      process.removeListener('SIGTERM', terminate)
      if (stderr) process.stderr.write(stderr)
      if (error?.code === 'ENOENT') {
        reject(new Error('Native gptgrep was not found. Install its release binary or set GPTGREP_BIN to its executable path.'))
        return
      }
      let data
      try {
        data = JSON.parse(stdout)
      } catch {
        reject(new Error(error
          ? `Native gptgrep failed (${error.signal || error.code || 'unknown'}); see stderr.`
          : 'Native gptgrep did not return valid JSON.'))
        return
      }
      if (data === null || typeof data !== 'object' || Array.isArray(data)) {
        reject(new Error('Native gptgrep returned a non-object JSON response.'))
        return
      }
      // A grep-style no-match status is only successful with a recognizable
      // empty result payload. Never convert arbitrary exit-1 errors to success.
      const matches = data.hits ?? data.results
      const noMatch = args[0] === 'search' && error?.code === 1
        && Array.isArray(matches) && matches.length === 0
        && data.ok !== false && !Object.hasOwn(data, 'error')
      if (error && !noMatch) {
        reject(new Error(`Native gptgrep failed (${error.signal || error.code || 'unknown'}); see stderr.`))
        return
      }
      resolve(data)
    })
    const interrupt = () => child.kill('SIGINT')
    const terminate = () => child.kill('SIGTERM')
    process.once('SIGINT', interrupt)
    process.once('SIGTERM', terminate)
  })
}
