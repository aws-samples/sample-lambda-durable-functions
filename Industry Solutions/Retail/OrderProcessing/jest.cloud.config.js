module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.cloud.test.ts'],
  transform: {
    '^.+\\.tsx?$': 'ts-jest'
  },
  // Cloud tests run against deployed Lambda functions — allow generous timeouts
  testTimeout: 300000, // 5 minutes default
};
