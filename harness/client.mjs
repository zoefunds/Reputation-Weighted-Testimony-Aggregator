import fs from "fs";
import { createClient, createAccount } from "genlayer-js";
import { studionet } from "genlayer-js/chains";

const PK = fs.readFileSync(new URL("./pk.txt", import.meta.url), "utf8").trim();
const CONTRACT = "0xB801B1BC9797dbE65F35DCD07b2b6df302707fC9";

export const account = createAccount(PK);
export const client = createClient({ chain: studionet, account });
export const CONTRACT_ADDRESS = CONTRACT;

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function write(method, args, value = 0n) {
  const hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: method,
    args,
    value,
  });
  await sleep(3000);
  const receipt = await client.waitForTransactionReceipt({
    hash,
    retries: 200,
    interval: 7000,
  });
  return { hash, receipt };
}

export async function read(method, args = []) {
  await sleep(2200);
  return client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: method,
    args,
  });
}
