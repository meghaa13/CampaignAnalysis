import pandas as pd
def extract_location_parts(canonical_name):
    parts = [p.strip() for p in canonical_name.split(",")]
    # Pad to length 3 if needed
    while len(parts) < 3:
        parts.append("")
    return pd.Series({
        "City": parts[0],
        "Region": parts[1],
        "Country": parts[2]
    })

print(extract_location_parts("Srinagar,Jammu and Kashmir,India"))
# Output: City: Srinagar, Region: Jammu and Kashmir, Country: India
