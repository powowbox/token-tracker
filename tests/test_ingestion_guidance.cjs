const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'),'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('function ingestionGuidance('),source.indexOf('function localIngestionTime(')),context);
const check = context.ingestionGuidance;
const now = Date.parse('2026-09-07T12:00:00Z');
const start = '2026-09-06T22:00:00Z';
test('no history, before period, incomplete period and covered historical period',()=>{
 assert.match(check(null,start,null,now),/No data has been imported/);
 assert.match(check('2026-09-06T10:00:00Z',start,null,now),/not been refreshed for this period/);
 assert.match(check('2026-09-07T08:00:00Z',start,null,now),/during this period/);
 assert.equal(check('2026-09-06T21:59:59Z','2026-09-05T22:00:00Z','2026-09-06T21:59:59Z',now),'');
});
test('ongoing range uses five-minute grace; historical ranges require coverage',()=>{
 assert.equal(check('2026-09-07T11:55:00Z',start,'2026-09-07T12:00:00Z',now),'');
 assert.match(check('2026-09-07T11:54:59Z',start,null,now),/More recent/);
 assert.match(check('2026-09-06T21:59:58Z',start,'2026-09-06T21:59:59Z',now),/Refresh usage/);
 assert.equal(check('2026-09-07T13:00:00+02:00',start,'2026-09-07T11:00:00Z',now),'');
});
