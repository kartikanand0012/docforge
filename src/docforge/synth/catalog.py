"""Invented reference data for the generator.

Company names, PANs and licence numbers are fictional. Product names are generic
(pharmacopoeial) names. HSN codes and GST rates are plausible but have not been checked
against the tariff schedule; they exist to give the documents realistic variety.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class State:
    name: str
    code: str
    abbr: str


@dataclass(frozen=True)
class Company:
    code: str
    name: str
    address: str
    state: State
    pan: str
    licence_serial: int


@dataclass(frozen=True)
class Product:
    name: str
    pack: str
    hsn: str
    gst_rate: Decimal
    mrp: Decimal


MH = State("Maharashtra", "27", "MH")
GJ = State("Gujarat", "24", "GJ")
KA = State("Karnataka", "29", "KA")
DL = State("Delhi", "07", "DL")
TN = State("Tamil Nadu", "33", "TN")

# Wholesale distributors (licence forms 20B / 21B).
SELLERS: tuple[Company, ...] = (
    Company(
        "SRP",
        "Shreeram Pharma Distributors",
        "14 Market Yard Road, Pune 411037",
        MH,
        "AAKCS4821M",
        104512,
    ),
    Company(
        "NVM",
        "Navjivan Medical Agencies",
        "3 Relief Road, Ahmedabad 380001",
        GJ,
        "AABFN7754K",
        208831,
    ),
    Company(
        "KLP",
        "Kaveri Lifecare Pharma",
        "88 Avenue Road, Bengaluru 560002",
        KA,
        "AAGCK3390P",
        310274,
    ),
    Company(
        "DMS",
        "Dilli Medisupply Pvt Ltd",
        "21 Bhagirath Palace, Delhi 110006",
        DL,
        "AAECD6612R",
        415960,
    ),
    Company(
        "CPA",
        "Coromandel Pharma Agency",
        "52 Nyniappa Street, Chennai 600003",
        TN,
        "AADFC9047H",
        527318,
    ),
)

# Retail chemists (licence forms 20 / 21).
BUYERS: tuple[Company, ...] = (
    Company("ARM", "Arogya Medical Stores", "7 Laxmi Road, Pune 411030", MH, "ABKPA5521C", 611204),
    Company(
        "SWC",
        "Sanjeevani Wellness Chemist",
        "112 Linking Road, Mumbai 400050",
        MH,
        "AAVFS2087J",
        623377,
    ),
    Company("UMS", "Umiya Medical Store", "9 Station Road, Surat 395003", GJ, "ACDPU8840L", 634519),
    Company(
        "JPH", "Jalaram Pharmacy", "45 Raopura Main Road, Vadodara 390001", GJ, "AAHFJ1196Q", 645082
    ),
    Company(
        "NMC",
        "Nandi Medicals and Chemists",
        "30 Sayyaji Rao Road, Mysuru 570001",
        KA,
        "ABXPN7302D",
        656741,
    ),
    Company(
        "BLP",
        "Basava Lifeline Pharmacy",
        "18 Jayanagar 4th Block, Bengaluru 560011",
        KA,
        "AANFB4458E",
        667925,
    ),
    Company(
        "YMH", "Yamuna Medicine House", "64 Lajpat Nagar II, Delhi 110024", DL, "ACFPY6639G", 678310
    ),
    Company(
        "RKC", "Rajdhani Chemists", "5 Karol Bagh Main Road, Delhi 110005", DL, "AARFR3014N", 689476
    ),
    Company(
        "VMP",
        "Vaigai Medical Pharmacy",
        "27 West Masi Street, Madurai 625001",
        TN,
        "ADLPV9925B",
        690158,
    ),
    Company(
        "MMS", "Marina Medical Stores", "71 Pondy Bazaar, Chennai 600017", TN, "AAUFM5873T", 701264
    ),
)

PRODUCTS: tuple[Product, ...] = (
    Product("Paracetamol Tablets IP 500mg", "10x10", "30049099", Decimal(12), Decimal("32.50")),
    Product("Amoxicillin Capsules IP 250mg", "10x10", "30041030", Decimal(12), Decimal("78.40")),
    Product("Azithromycin Tablets IP 500mg", "1x3", "30042019", Decimal(12), Decimal("119.00")),
    Product("Cetirizine Tablets IP 10mg", "10x10", "30049099", Decimal(12), Decimal("21.75")),
    Product("Metformin Tablets IP 500mg", "10x15", "30049099", Decimal(5), Decimal("46.20")),
    Product("Amlodipine Tablets IP 5mg", "10x10", "30049099", Decimal(5), Decimal("38.90")),
    Product("Atorvastatin Tablets IP 10mg", "10x10", "30049099", Decimal(5), Decimal("96.00")),
    Product("Pantoprazole Tablets IP 40mg", "10x10", "30049099", Decimal(12), Decimal("112.50")),
    Product("Omeprazole Capsules IP 20mg", "10x10", "30049099", Decimal(12), Decimal("64.30")),
    Product("Ibuprofen Tablets IP 400mg", "10x10", "30049099", Decimal(12), Decimal("28.60")),
    Product("Prednisolone Tablets IP 5mg", "10x10", "30043200", Decimal(12), Decimal("17.85")),
    Product("Vitamin C Tablets 500mg", "1x15", "30045090", Decimal(18), Decimal("54.00")),
    Product("Ciprofloxacin Tablets IP 500mg", "10x10", "30042019", Decimal(12), Decimal("88.75")),
    Product("ORS Powder WHO Formula 21g", "1x25", "30049099", Decimal(5), Decimal("23.10")),
    Product("Salbutamol Inhaler 100mcg", "1x1", "30049099", Decimal(12), Decimal("148.00")),
    Product("Insulin Injection IP 40IU/ml", "1x10ml", "30043110", Decimal(5), Decimal("171.60")),
)

# `None` means no free-goods scheme; "10+1" means one free pack per ten bought.
SCHEMES: tuple[str | None, ...] = (None, None, None, "10+1", "10+1", "12+1")
QUANTITIES: tuple[int, ...] = (10, 20, 24, 30, 50, 60, 100, 120, 200)
DISCOUNTS: tuple[Decimal, ...] = (
    Decimal(0),
    Decimal(0),
    Decimal(0),
    Decimal(2),
    Decimal("2.5"),
    Decimal(5),
)
