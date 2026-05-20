/**
 * CLI command router using node:util parseArgs.
 * Zero dependencies — uses only Node.js built-ins.
 *
 * data-svc patch: see writeAndExit() below. Mirror of
 * external/tradingview-mcp/src/cli/router.js — copied verbatim except for the
 * drain-before-exit fix in execute()/handleError(). See BUG_tv_non_json.md.
 */
import { parseArgs } from 'node:util';

/** @type {Map<string, { description: string, options?: object, handler: Function, subcommands?: Map<string, object> }>} */
const commands = new Map();

export function register(name, config) {
  commands.set(name, config);
}

function printHelp() {
  console.log('Usage: tv <command> [options]\n');
  console.log('Commands:');
  const maxLen = Math.max(...[...commands.keys()].map(k => k.length));
  for (const [name, cmd] of commands) {
    if (cmd.subcommands) {
      const subs = [...cmd.subcommands.keys()].join(', ');
      console.log(`  ${name.padEnd(maxLen + 2)}${cmd.description}  [${subs}]`);
    } else {
      console.log(`  ${name.padEnd(maxLen + 2)}${cmd.description}`);
    }
  }
  console.log('\nRun "tv <command> --help" for command-specific options.');
  console.log('\nDISCLAIMER');
  console.log('  Not affiliated with TradingView Inc. or Anthropic, PBC.');
  console.log('  Use subject to TradingView\'s Terms of Use: tradingview.com/policies');
}

function printCommandHelp(name, cmd) {
  if (cmd.subcommands) {
    console.log(`Usage: tv ${name} <subcommand> [options]\n`);
    console.log('Subcommands:');
    for (const [sub, subConf] of cmd.subcommands) {
      console.log(`  ${sub.padEnd(12)}${subConf.description}`);
    }
  } else {
    console.log(`Usage: tv ${name} [options]\n`);
    console.log(cmd.description);
  }
  const opts = cmd.options || {};
  if (Object.keys(opts).length > 0) {
    console.log('\nOptions:');
    for (const [k, v] of Object.entries(opts)) {
      const flag = v.short ? `-${v.short}, --${k}` : `    --${k}`;
      console.log(`  ${flag.padEnd(20)}${v.description || ''}`);
    }
  }
}

export async function run(argv) {
  const args = argv.slice(2);

  if (args.length === 0 || args[0] === '--help' || args[0] === '-h') {
    printHelp();
    process.exit(0);
  }

  const cmdName = args[0];
  const cmd = commands.get(cmdName);

  if (!cmd) {
    console.error(`Unknown command: ${cmdName}`);
    console.error('Run "tv --help" for a list of commands.');
    process.exit(1);
  }

  // Handle subcommands (e.g., tv pine get)
  let handler, options;
  if (cmd.subcommands) {
    const subName = args[1];
    if (!subName || subName === '--help' || subName === '-h') {
      printCommandHelp(cmdName, cmd);
      process.exit(0);
    }
    const sub = cmd.subcommands.get(subName);
    if (!sub) {
      console.error(`Unknown subcommand: ${cmdName} ${subName}`);
      printCommandHelp(cmdName, cmd);
      process.exit(1);
    }
    handler = sub.handler;
    options = sub.options || {};
    // Parse remaining args after command + subcommand
    try {
      const { values, positionals } = parseArgs({
        args: args.slice(2),
        options: { help: { type: 'boolean', short: 'h' }, ...options },
        allowPositionals: true,
        strict: false,
      });
      if (values.help) {
        console.log(`Usage: tv ${cmdName} ${subName} [options]\n`);
        console.log(sub.description);
        if (Object.keys(options).length > 0) {
          console.log('\nOptions:');
          for (const [k, v] of Object.entries(options)) {
            const flag = v.short ? `-${v.short}, --${k}` : `    --${k}`;
            console.log(`  ${flag.padEnd(20)}${v.description || ''}`);
          }
        }
        process.exit(0);
      }
      await execute(handler, values, positionals);
    } catch (err) {
      handleError(err);
    }
  } else {
    handler = cmd.handler;
    options = cmd.options || {};
    try {
      const { values, positionals } = parseArgs({
        args: args.slice(1),
        options: { help: { type: 'boolean', short: 'h' }, ...options },
        allowPositionals: true,
        strict: false,
      });
      if (values.help) {
        printCommandHelp(cmdName, cmd);
        process.exit(0);
      }
      await execute(handler, values, positionals);
    } catch (err) {
      handleError(err);
    }
  }
}

// data-svc patch — drain before exit.
// console.log/error followed immediately by process.exit() TRUNCATES large
// payloads when the stream is a pipe: Node buffers pipe writes asynchronously
// and process.exit() terminates the process before the buffer flushes. This is
// the root cause of the intermittent "tv CLI returned non-JSON" the data-svc
// hit on the ~76 KB / 500-bar `ohlcv` output. Writing with a completion
// callback and only then exiting guarantees the full payload reaches the pipe.
// See BUG_tv_non_json.md.
function writeAndExit(stream, text, code) {
  stream.write(text + '\n', () => process.exit(code));
}

async function execute(handler, values, positionals) {
  try {
    const result = await handler(values, positionals);
    writeAndExit(process.stdout, JSON.stringify(result, null, 2), 0);
  } catch (err) {
    handleError(err);
  }
}

function handleError(err) {
  const message = err.message || String(err);
  // Connection failures get exit code 2
  const code = /CDP|connection|ECONNREFUSED|not running/i.test(message) ? 2 : 1;
  writeAndExit(process.stderr, JSON.stringify({ success: false, error: message }, null, 2), code);
}
