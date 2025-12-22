from typing import List, Dict, Any, Union

class DiagnosisManager:
    def __init__(self):

        # Main recursive diagnosis pool
        self.diagnoses = []
        

        

    # ---------------------------------------------------------
    # CONSOLIDATED DIAGNOSIS FUNCTIONS
    # ---------------------------------------------------------
    def get_diagnoses(self) -> List[Dict[str, Any]]:
        """Returns the full consolidated list with metrics."""
        ranked_d = []
        for i, d in enumerate(self.diagnoses):
            d['rank'] = i + 1
            points = len(d.get('indicators_point', [])) 

            # 1. HIGH: Must be Rank 1 (index 0) AND have > 8 points
            if (i == 0) and (points > 8):
                d['severity'] = "High"
                
            # 2. MODERATE: Points > 5 (This covers 6, 7, 8, AND >8 if Rank is not 1)
            elif points > 5:
                d['severity'] = "Moderate"
                
            # 3. LOW: Points 4, 5
            elif points > 3:
                d['severity'] = "Low"
                
            # 4. VERY LOW: Points <= 3
            else:
                d['severity'] = "Very Low"

            ranked_d.append(d)
        return ranked_d

    def get_diagnoses_basic(self) -> List[Dict[str, Any]]:
        """
        Returns the consolidated list EXCLUDING 'indicators_count' and 'probability'.
        Keys returned: did, diagnosis, indicators_point.
        """
        simplified_list = []
        for item in self.diagnoses:
            simplified_list.append({
                "did": item["did"],
                "diagnosis": item["diagnosis"],
                "indicators_point": item["indicators_point"]
            })
        return simplified_list


