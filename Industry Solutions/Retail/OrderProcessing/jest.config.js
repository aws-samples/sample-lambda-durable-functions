module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  testPathIgnorePatterns: ['.*\\.cloud\\.test\\.ts$'],
  transform: {
    '^.+\\.tsx?$': 'ts-jest'
  }
};
