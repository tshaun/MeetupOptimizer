import pandas as pd
from sklearn.cluster import KMeans
import numpy as np

# Load and clean the CSV (assuming well-structured columns: 'Name', 'Type', 'Cost', 'Duration', 'URL')
df = pd.read_csv('SMT481.csv')
# If columns need parsing (see earlier error), apply more regex or manual cleaning

# Suppose you have coordinates for each venue (Latitude, Longitude)
# Here we use random numbers for demonstration (replace with actual geocoding in production)
np.random.seed(42)
df['Latitude'] = np.random.uniform(1.28, 1.38, size=len(df))
df['Longitude'] = np.random.uniform(103.7, 104.0, size=len(df))

# Cluster venues by location
num_clusters = min(4, len(df) // 3)
coords = df[['Latitude', 'Longitude']]
kmeans = KMeans(n_clusters=num_clusters, random_state=42)
df['Cluster'] = kmeans.fit_predict(coords)

# For each cluster, apply greedy venue selection (subject to time/budget constraint)
max_time = 2.0  # total allowed hours for route
max_budget = 50  # total allowed budget for cluster

results = {}
for cluster in range(num_clusters):
    group = df[df['Cluster'] == cluster]
    group = group.sort_values(by=['Cost', 'Duration'])  # adjust for your utility metric
    chosen = []
    total_time, total_cost = 0.0, 0.0
    for _, row in group.iterrows():
        duration = float(str(row['Duration']).replace('h',''))  # parse duration string
        cost = float(row['Cost'])
        if total_time + duration <= max_time and total_cost + cost <= max_budget:
            chosen.append(row['Name'])
            total_time += duration
            total_cost += cost
    results[cluster] = {'Venues': chosen, 'Time': total_time, 'Cost': total_cost}

# Optional: Save/print cluster route tables
for cid, data in results.items():
    print(f'Cluster {cid}: Venues: {data["Venues"]}, Total Time: {data["Time"]}, Total Cost: {data["Cost"]}')


# Clustered metaheuristics algorithm:
# The workflow includes clustering venues by latitude/longitude, then applying a greedy heuristic to select venues in each cluster that maximize utility while respecting your scheduling constraints.

# Solution Steps
# Parse your dataset: Extract fields—venue name, type/tag, cost, duration, and Google Maps URL.​
# Geocode venues: Use Google Maps API or a similar service to add latitude/longitude for each venue, which allows spatial clustering.
# Cluster venues: Group venues by spatial proximity (KMeans on coordinates) or by type/tag if only textual data is available.
# Apply metaheuristics: Within each cluster, perform a greedy selection where you pick venues to maximize utility under total time and cost constraints. In practice, this greedy step may be swapped for more advanced metaheuristics if desired.
# Aggregate results: Combine chosen routes from each cluster, ensuring global feasibility for time and budget limits.



# The output will be clusters of venues chosen under time and budget constraints, printed to your console.

# For further validation, adjust max_time and max_budget to simulate different user scenarios, or print the selected venues for each cluster to verify logical grouping/selection.

# Check that:
# Output venues fit within the cluster’s limits.
# No cluster exceeds total time or cost limits.
# Each cluster contains a logical spatial grouping (if proper coordinates are used).