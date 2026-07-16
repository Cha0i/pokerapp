const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class TestDOMParser {
    parseFromString(source) {
        const match = source.match(/<body\b[^>]*>([\s\S]*?)<\/body>/i);
        const textContent = match
            ? match[1]
                .replace(/&quot;/g, '"')
                .replace(/&apos;/g, "'")
                .replace(/&lt;/g, '<')
                .replace(/&gt;/g, '>')
                .replace(/&amp;/g, '&')
            : '';
        return {
            getElementsByTagName(name) {
                return name === 'body' && match ? [{ textContent }] : [];
            }
        };
    }
}

const quietConsole = {
    log() {},
    info() {},
    warn() {},
    error() {},
    debug() {}
};
const windowValue = {
    console: quietConsole,
    location: {
        hostname: 'example.test',
        origin: 'https://example.test',
        pathname: '/',
        href: 'https://example.test/'
    },
    addEventListener() {},
    setInterval() { return 1; },
    setTimeout() { return 1; }
};
windowValue.self = windowValue;
windowValue.top = windowValue;

const context = {
    console: quietConsole,
    window: windowValue,
    unsafeWindow: windowValue,
    DOMParser: TestDOMParser,
    GM_registerMenuCommand() {},
    GM_xmlhttpRequest() {}
};
context.globalThis = context;

const userscriptPath = path.join(__dirname, '..', 'tampermonkey-bridge.user.js');
let source = fs.readFileSync(userscriptPath, 'utf8');
source = source.replace(
    '    // Public helper for console/manual automation.',
    '    globalThis.__relaxParserTestApi = {\n' +
    '        parseRelaxFrameBodies: parseRelaxFrameBodies,\n' +
    '        payloadFromRelaxBody: payloadFromRelaxBody,\n' +
    '        redactSensitiveText: redactSensitiveText\n' +
    '    };\n\n' +
    '    // Public helper for console/manual automation.'
);
vm.runInNewContext(source, context, { filename: userscriptPath });

const parse = context.__relaxParserTestApi.payloadFromRelaxBody;
const parseBodies = context.__relaxParserTestApi.parseRelaxFrameBodies;
const redact = context.__relaxParserTestApi.redactSensitiveText;
const tableWithNames = (names, states, board = null, bets = [0, 0, 0, 0, 0, 0], pots = []) => [
    names,
    states,
    [100, 100, 100, 100, 100, 100],
    bets,
    pots,
    null,
    null,
    board
];
const table = (states, board = null, bets = [0, 0, 0, 0, 0, 0], pots = []) => (
    tableWithNames('p0|p1|xtlx|p3|p4|p5', states, board, bets, pots)
);
const playerAt = (seat, hole) => ['table-instance', seat, 0, hole, null, null];
const player = (hole) => playerAt(2, hole);

assert.equal(
    redact('{"relaxtoken":"secret","token":"also-secret"}'),
    '{"relaxtoken":"[redacted]","token":"[redacted]"}'
);

const frameBodies = parseBodies(
    '<message><body>{&quot;tags&quot;:[&quot;deal&quot;],&quot;payLoad&quot;:{&quot;hid&quot;:42}}</body></message>'
);
assert.deepEqual(JSON.parse(JSON.stringify(frameBodies)), [
    { tags: ['deal'], payLoad: { hid: 42 } }
]);

const init = parse({
    tags: ['init'],
    payLoad: { hid: 42, tid: 9001, c: table([1, 1, 1, 1, 1, 1]), p: player(null) }
});
assert.deepEqual(JSON.parse(JSON.stringify(init)), {
    type: 'poker_cards',
    handId: 42,
    tableId: 9001,
    reset: true,
    board: [],
    heroSeatId: 2,
    players: 6,
    heroSittingOut: false,
    heroFolded: false,
    heroTurn: false,
    pot: 0,
    toCall: 0
});

const deal = parse({
    tags: ['deal'],
    payLoad: { hid: 42, tid: 9001, c: table([1, 1, 1, 1, 1, 1]), p: player('kd4d') }
});
assert.deepEqual(JSON.parse(JSON.stringify(deal)), {
    type: 'poker_cards',
    handId: 42,
    tableId: 9001,
    heroSeatId: 2,
    hole: ['Kd', '4d'],
    board: [],
    heroSittingOut: false,
    heroFolded: false,
    heroTurn: false
});

const villainDeal = parse({
    tags: ['deal'],
    payLoad: {
        hid: 43,
        tid: 9001,
        c: tableWithNames('xtlx|villain|p2|p3|p4|p5', [1, 1, 1, 1, 1, 1]),
        p: playerAt(2, 'asah')
    }
});
assert.equal(Object.prototype.hasOwnProperty.call(villainDeal, 'hole'), false);
assert.equal(villainDeal.heroSeatId, 0);

const heroDecision = parse({
    tags: ['pturn'],
    payLoad: {
        hid: 43,
        tid: 9001,
        c: table([1, 1, 1, 1, 1, 1], null, [0, 2, 4, 0, 4, 8]),
        d: [2, 15, 0, [[0, 0], [2, 4], [3, 8, 96, [8, 12, 16, 20]]]],
        p: player('kd4d')
    }
});
assert.deepEqual(JSON.parse(JSON.stringify(heroDecision)), {
    type: 'poker_cards',
    handId: 43,
    tableId: 9001,
    heroSeatId: 2,
    heroTurn: true,
    pot: 18,
    toCall: 4,
    minimumRaise: 8,
    hole: ['Kd', '4d'],
    board: [],
    heroSittingOut: false,
    heroFolded: false
});

const flop = parse({
    tags: ['flop'],
    payLoad: {
        hid: 43,
        tid: 9001,
        c: table([3, 3, 1, 3, 1, 1], '9s2d9h', [0, 0, 0, 0, 0, 0], [[25, 1]]),
        p: player('kd4d')
    }
});
assert.deepEqual(JSON.parse(JSON.stringify(flop)), {
    type: 'poker_cards',
    handId: 43,
    tableId: 9001,
    heroSeatId: 2,
    players: 3,
    board: ['9s', '2d', '9h'],
    heroSittingOut: false,
    heroFolded: false,
    heroTurn: false,
    pot: 25,
    toCall: 0
});

const folded = parse({
    tags: ['act'],
    payLoad: {
        hid: 43,
        tid: 9001,
        c: table([3, 3, 3, 3, 1, 1], '9s2d9h', [0, 0, 0, 0, 0, 0], [[25, 1]]),
        d: [2, 0, 0],
        p: player(null)
    }
});
assert.equal(folded.players, 2);
assert.equal(folded.heroFolded, true);
assert.equal(Object.prototype.hasOwnProperty.call(folded, 'hole'), false);
assert.deepEqual(Array.from(folded.board), ['9s', '2d', '9h']);

console.log('Relax userscript parser tests passed');
