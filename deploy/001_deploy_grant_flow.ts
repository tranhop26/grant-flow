/**
 * GrantFlow deploy script — run with `genlayer deploy`.
 * Network selection follows your genlayer CLI configuration
 * (`genlayer network` to switch between studionet / testnets).
 */
import { readFileSync } from "fs";
import path from "path";
import {
  TransactionHash,
  GenLayerClient,
} from "genlayer-js/types";

// ---- customize your DAO here -------------------------------------------
const DAO_NAME = "GenLayer Community Grants";
const CRITERIA = `We fund projects that grow the GenLayer ecosystem:
- open-source tooling, analytics and infrastructure for Intelligent Contracts
- educational content and developer onboarding
- dApps that showcase LLM + web-access capabilities
We value working prototypes over promises, clear budgets, public repos,
and teams with verifiable track records. Red flags: vague scope,
closed source without justification, unrealistic timelines.`;
const MIN_TOTAL_SCORE = 30;      // of 50
const MIN_CONFIDENCE = 70;       // of 100
const SUBMIT_COOLDOWN_SECS = 3600;
// ------------------------------------------------------------------------

export default async function main(client: GenLayerClient<any>) {
  const filePath = path.resolve(process.cwd(), "contracts/grant_flow.py");
  const contractCode = new Uint8Array(readFileSync(filePath));

  await client.initializeConsensusSmartContract();

  const deployTransaction = await client.deployContract({
    code: contractCode,
    args: [DAO_NAME, CRITERIA, MIN_TOTAL_SCORE, MIN_CONFIDENCE, SUBMIT_COOLDOWN_SECS],
  });

  const receipt = await client.waitForTransactionReceipt({
    hash: deployTransaction as TransactionHash,
    retries: 200,
  });

  const address =
    (receipt as any)?.data?.contract_address ??
    (receipt as any)?.contractAddress ??
    receipt;
  console.log("GrantFlow deployed:", address);
  return address;
}
