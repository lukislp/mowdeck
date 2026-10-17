// The part of ua-parser-js 0.7 (no bundled types) that src/user-agent.test.ts uses.
declare module 'ua-parser-js' {
  interface Part {
    name?: string;
    version?: string;
    major?: string;
  }
  export default class UAParser {
    constructor(ua?: string);
    getResult(): { browser: Part; os: Part; engine: Part };
  }
}
