"""Generate synthetic ASA Detections XML for benchmarking.

Every document and deployment is clearly marked SYN_. The data is random and
means nothing biologically. Its only purpose is to give realistic volume and
shape: call-level detections carrying the per-detection parameters a
PAMGuard/Raven workflow typically exports.

  python bench/synth.py OUTDIR --deployments 200 --per-deployment 5000
"""

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

NS = "http://tethys.sdsu.edu/schema/1.0"
# A handful of ITIS TSNs, with a typical call label and frequency range (Hz).
SPECIES = [
    (180473, "Clicks", 110_000, 150_000),
    (180404, "Moan", 20, 200),
    (180530, "Whistle", 5_000, 20_000),
    (180498, "Clicks", 20_000, 60_000),
    (770799, "Clicks", 25_000, 50_000),
]


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def make_doc(dep_id: str, n: int, rng: random.Random, start: datetime, days: int) -> str:
    species = rng.sample(SPECIES, k=rng.randint(2, 4))
    end = start + timedelta(days=days)
    span = (end - start).total_seconds()
    times = sorted(rng.uniform(0, span - 60) for _ in range(n))

    out = [f'<?xml version="1.0" encoding="UTF-8"?>\n<Detections xmlns="{NS}">\n',
           f"   <Id>{dep_id}_detections</Id>\n",
           f"   <DataSource>\n      <DeploymentId>{dep_id}</DeploymentId>\n   </DataSource>\n",
           "   <Algorithm>\n      <Method>Synthetic</Method>\n      <Software>nereus-synth</Software>\n"
           "      <Version>0</Version>\n      <Parameters/>\n   </Algorithm>\n",
           "   <UserId>synthetic</UserId>\n   <Effort>\n",
           f"      <Start>{ts(start)}</Start>\n      <End>{ts(end)}</End>\n"]
    for tsn, call, _, _ in species:
        out.append(f"      <Kind>\n         <SpeciesId>{tsn}</SpeciesId>\n"
                   f"         <Call>{call}</Call>\n         <Granularity>call</Granularity>\n"
                   f"      </Kind>\n")
    out.append("   </Effort>\n   <OnEffort>\n")
    for off in times:
        tsn, call, lo, hi = rng.choice(species)
        t0 = start + timedelta(seconds=off)
        dur = rng.uniform(0.05, 4.0)
        f1 = rng.uniform(lo, (lo + hi) / 2)
        f2 = rng.uniform((lo + hi) / 2, hi)
        out.append(
            "      <Detection>\n"
            f"         <Start>{ts(t0)}</Start>\n"
            f"         <End>{ts(t0 + timedelta(seconds=dur))}</End>\n"
            f"         <Channel>{rng.randint(0, 3)}</Channel>\n"
            f"         <SpeciesId>{tsn}</SpeciesId>\n"
            f"         <Call>{call}</Call>\n"
            "         <Parameters>\n"
            f"            <Score>{rng.random():.4f}</Score>\n"
            f"            <ReceivedLevel_dB>{rng.uniform(90, 150):.1f}</ReceivedLevel_dB>\n"
            f"            <SNR_dB>{rng.uniform(3, 30):.1f}</SNR_dB>\n"
            f"            <MinFreq_Hz>{f1:.0f}</MinFreq_Hz>\n"
            f"            <MaxFreq_Hz>{f2:.0f}</MaxFreq_Hz>\n"
            f"            <Duration_s>{dur:.3f}</Duration_s>\n"
            "         </Parameters>\n"
            "      </Detection>\n")
    out.append("   </OnEffort>\n</Detections>\n")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--deployments", type=int, default=200)
    ap.add_argument("--per-deployment", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    deployments = {}
    for i in range(a.deployments):
        dep = f"SYN_{i:04d}"
        # Scatter moorings over a North Atlantic box.
        lat, lon = rng.uniform(40, 62), rng.uniform(-40, 5)
        start = datetime(2019, 1, 1, tzinfo=timezone.utc) + timedelta(days=rng.randint(0, 900))
        days = rng.randint(60, 240)
        deployments[dep] = {"lat": lat, "lon": lon, "start": start.isoformat(),
                            "end": (start + timedelta(days=days)).isoformat()}
        n = max(1, int(rng.gauss(a.per_deployment, a.per_deployment * 0.3)))
        (out / f"{dep}_detections.xml").write_text(make_doc(dep, n, rng, start, days))
    (out / "deployments.json").write_text(json.dumps(deployments, indent=1))
    print(f"wrote {a.deployments} documents to {out}")


if __name__ == "__main__":
    main()
