"""Provenance-driven citation engine for the report generator.

Design goals (see docs/plan):
- Citations are derived from the *typed provenance fields* of each job
  (engine / functional / basis / force field / charge method / solvent model /
  pseudopotentials), NOT from substring-scanning a rendered config string.
  This kills the old bugs where "B3LYP" matched nothing and "AMBER" was
  mis-cited as GAFF.
- References are deduplicated and *numbered* by first appearance, and rendered
  as a proper numbered reference list with DOIs.
- Every entry in REFERENCE_LIBRARY is a real, verifiable citation with a DOI
  (or a canonical book/software citation).

Public API used by the new pipeline:
- ``resolve_provenance_citations(...)`` -> ordered unique keys for one job.
- ``MODULE_DEFAULT_CITATIONS`` -> baseline keys per module kind.
- ``build_reference_map(keys)`` -> {key: number} by first appearance.
- ``reference_entries(ref_map)`` -> numbered list for the References section.
"""

from typing import Dict, Iterable, List, Optional


REFERENCE_LIBRARY: Dict[str, Dict[str, str]] = {
    # --- MD engines / force fields / charges ---
    "lammps": {
        "label": "Thompson et al., 2022",
        "reference": "Thompson, A. P. et al. LAMMPS - a flexible simulation tool for particle-based materials modeling at the atomic, meso, and continuum scales. Comput. Phys. Commun. 271, 108171 (2022). DOI: 10.1016/j.cpc.2021.108171.",
    },
    "lammps1995": {
        "label": "Plimpton, 1995",
        "reference": "Plimpton, S. Fast parallel algorithms for short-range molecular dynamics. J. Comput. Phys. 117, 1-19 (1995). DOI: 10.1006/jcph.1995.1039.",
    },
    "oplsaa": {
        "label": "Jorgensen et al., 1996",
        "reference": "Jorgensen, W. L.; Maxwell, D. S.; Tirado-Rives, J. Development and testing of the OPLS all-atom force field on conformational energetics and properties of organic liquids. J. Am. Chem. Soc. 118, 11225-11236 (1996). DOI: 10.1021/ja9621760.",
    },
    "gaff": {
        "label": "Wang et al., 2004",
        "reference": "Wang, J.; Wolf, R. M.; Caldwell, J. W.; Kollman, P. A.; Case, D. A. Development and testing of a general Amber force field. J. Comput. Chem. 25, 1157-1174 (2004). DOI: 10.1002/jcc.20035.",
    },
    "amber": {
        "label": "Maier et al., 2015",
        "reference": "Maier, J. A. et al. ff14SB: improving the accuracy of protein side chain and backbone parameters from ff99SB. J. Chem. Theory Comput. 11, 3696-3713 (2015). DOI: 10.1021/acs.jctc.5b00255.",
    },
    "ligpargen": {
        "label": "Dodda et al., 2017",
        "reference": "Dodda, L. S.; Cabeza de Vaca, I.; Tirado-Rives, J.; Jorgensen, W. L. LigParGen web server: an automatic OPLS-AA parameter generator for organic ligands. Nucleic Acids Res. 45, W331-W336 (2017). DOI: 10.1093/nar/gkx312.",
    },
    "resp": {
        "label": "Bayly et al., 1993",
        "reference": "Bayly, C. I.; Cieplak, P.; Cornell, W.; Kollman, P. A. A well-behaved electrostatic potential based method using charge restraints for deriving atomic charges: the RESP model. J. Phys. Chem. 97, 10269-10280 (1993). DOI: 10.1021/j100142a004.",
    },
    "ecc": {
        "label": "Leontyev & Stuchebrukhov, 2011",
        "reference": "Leontyev, I.; Stuchebrukhov, A. Accounting for electronic polarization in non-polarizable force fields. Phys. Chem. Chem. Phys. 13, 2613-2626 (2011). DOI: 10.1039/c0cp01971b.",
    },
    # --- Transport / analysis methodology ---
    "einstein_msd": {
        "label": "Allen & Tildesley, 2017",
        "reference": "Allen, M. P.; Tildesley, D. J. Computer Simulation of Liquids, 2nd ed.; Oxford University Press: Oxford, 2017. DOI: 10.1093/oso/9780198803195.001.0001.",
    },
    "nernst_einstein": {
        "label": "France-Lanord & Grossman, 2019",
        "reference": "France-Lanord, A.; Grossman, J. C. Correlations from ion pairing and the Nernst-Einstein equation. Phys. Rev. Lett. 122, 136001 (2019). DOI: 10.1103/PhysRevLett.122.136001.",
    },
    # --- Quantum chemistry engines ---
    "pyscf": {
        "label": "Sun et al., 2018",
        "reference": "Sun, Q. et al. PySCF: the Python-based simulations of chemistry framework. WIREs Comput. Mol. Sci. 8, e1340 (2018). DOI: 10.1002/wcms.1340.",
    },
    "pyscf2020": {
        "label": "Sun et al., 2020",
        "reference": "Sun, Q. et al. Recent developments in the PySCF program package. J. Chem. Phys. 153, 024109 (2020). DOI: 10.1063/5.0006074.",
    },
    "gaussian": {
        "label": "Frisch et al., 2016",
        "reference": "Frisch, M. J. et al. Gaussian 16, Revision C.01. Gaussian, Inc., Wallingford CT (2016).",
    },
    "orca": {
        "label": "Neese, 2022",
        "reference": "Neese, F. Software update: the ORCA program system, version 5.0. WIREs Comput. Mol. Sci. 12, e1606 (2022). DOI: 10.1002/wcms.1606.",
    },
    "xtb": {
        "label": "Bannwarth et al., 2019",
        "reference": "Bannwarth, C.; Ehlert, S.; Grimme, S. GFN2-xTB: an accurate and broadly parametrized self-consistent tight-binding quantum chemical method with multipole electrostatics and density-dependent dispersion contributions. J. Chem. Theory Comput. 15, 1652-1671 (2019). DOI: 10.1021/acs.jctc.8b01176.",
    },
    # --- Density functionals ---
    "pbe": {
        "label": "Perdew et al., 1996",
        "reference": "Perdew, J. P.; Burke, K.; Ernzerhof, M. Generalized gradient approximation made simple. Phys. Rev. Lett. 77, 3865-3868 (1996). DOI: 10.1103/PhysRevLett.77.3865.",
    },
    "pbe0": {
        "label": "Adamo & Barone, 1999",
        "reference": "Adamo, C.; Barone, V. Toward reliable density functional methods without adjustable parameters: the PBE0 model. J. Chem. Phys. 110, 6158-6170 (1999). DOI: 10.1063/1.478522.",
    },
    "b3lyp": {
        "label": "Stephens et al., 1994",
        "reference": "Stephens, P. J.; Devlin, F. J.; Chabalowski, C. F.; Frisch, M. J. Ab initio calculation of vibrational absorption and circular dichroism spectra using density functional force fields. J. Phys. Chem. 98, 11623-11627 (1994). DOI: 10.1021/j100096a001.",
    },
    "wb97xd": {
        "label": "Chai & Head-Gordon, 2008",
        "reference": "Chai, J.-D.; Head-Gordon, M. Long-range corrected hybrid density functionals with damped atom-atom dispersion corrections. Phys. Chem. Chem. Phys. 10, 6615-6620 (2008). DOI: 10.1039/b810189b.",
    },
    "m062x": {
        "label": "Zhao & Truhlar, 2008",
        "reference": "Zhao, Y.; Truhlar, D. G. The M06 suite of density functionals for main group thermochemistry, thermochemical kinetics, noncovalent interactions, excited states, and transition elements. Theor. Chem. Acc. 120, 215-241 (2008). DOI: 10.1007/s00214-007-0310-x.",
    },
    # --- Dispersion / basis sets / solvation ---
    "d3": {
        "label": "Grimme et al., 2010",
        "reference": "Grimme, S.; Antony, J.; Ehrlich, S.; Krieg, H. A consistent and accurate ab initio parametrization of density functional dispersion correction (DFT-D) for the 94 elements H-Pu. J. Chem. Phys. 132, 154104 (2010). DOI: 10.1063/1.3382344.",
    },
    "d3bj": {
        "label": "Grimme et al., 2011",
        "reference": "Grimme, S.; Ehrlich, S.; Goerigk, L. Effect of the damping function in dispersion corrected density functional theory. J. Comput. Chem. 32, 1456-1465 (2011). DOI: 10.1002/jcc.21759.",
    },
    "def2": {
        "label": "Weigend & Ahlrichs, 2005",
        "reference": "Weigend, F.; Ahlrichs, R. Balanced basis sets of split valence, triple zeta valence and quadruple zeta valence quality for H to Rn: design and assessment of accuracy. Phys. Chem. Chem. Phys. 7, 3297-3305 (2005). DOI: 10.1039/b508541a.",
    },
    "smd": {
        "label": "Marenich et al., 2009",
        "reference": "Marenich, A. V.; Cramer, C. J.; Truhlar, D. G. Universal solvation model based on solute electron density and on a continuum model of the solvent defined by the bulk dielectric constant and atomic surface tensions. J. Phys. Chem. B 113, 6378-6396 (2009). DOI: 10.1021/jp810292n.",
    },
    "pcm": {
        "label": "Tomasi et al., 2005",
        "reference": "Tomasi, J.; Mennucci, B.; Cammi, R. Quantum mechanical continuum solvation models. Chem. Rev. 105, 2999-3094 (2005). DOI: 10.1021/cr9904009.",
    },
    # --- Plane-wave / periodic DFT (surface adsorption, AIMD) ---
    "qe": {
        "label": "Giannozzi et al., 2009",
        "reference": "Giannozzi, P. et al. QUANTUM ESPRESSO: a modular and open-source software project for quantum simulations of materials. J. Phys.: Condens. Matter 21, 395502 (2009). DOI: 10.1088/0953-8984/21/39/395502.",
    },
    "qe2017": {
        "label": "Giannozzi et al., 2017",
        "reference": "Giannozzi, P. et al. Advanced capabilities for materials modelling with Quantum ESPRESSO. J. Phys.: Condens. Matter 29, 465901 (2017). DOI: 10.1088/1361-648X/aa8f79.",
    },
    "sssp": {
        "label": "Prandini et al., 2018",
        "reference": "Prandini, G. et al. Precision and efficiency in solid-state pseudopotential calculations. npj Comput. Mater. 4, 72 (2018). DOI: 10.1038/s41524-018-0127-2.",
    },
    "cp2k": {
        "label": "Kuehne et al., 2020",
        "reference": "Kuehne, T. D. et al. CP2K: an electronic structure and molecular dynamics software package - Quickstep: efficient and accurate electronic structure calculations. J. Chem. Phys. 152, 194103 (2020). DOI: 10.1063/5.0007045.",
    },
    "gth": {
        "label": "Goedecker et al., 1996",
        "reference": "Goedecker, S.; Teter, M.; Hutter, J. Separable dual-space Gaussian pseudopotentials. Phys. Rev. B 54, 1703-1710 (1996). DOI: 10.1103/PhysRevB.54.1703.",
    },
    "molopt": {
        "label": "VandeVondele & Hutter, 2007",
        "reference": "VandeVondele, J.; Hutter, J. Gaussian basis sets for accurate calculations on molecular systems in gas and condensed phases. J. Chem. Phys. 127, 114105 (2007). DOI: 10.1063/1.2770708.",
    },
    "aimd": {
        "label": "Marx & Hutter, 2009",
        "reference": "Marx, D.; Hutter, J. Ab Initio Molecular Dynamics: Basic Theory and Advanced Methods; Cambridge University Press: Cambridge, 2009. DOI: 10.1017/CBO9780511609633.",
    },
    # --- Solid-state electrolyte stability / NEB / ML potentials ---
    "materialsproject": {
        "label": "Jain et al., 2013",
        "reference": "Jain, A. et al. Commentary: The Materials Project: a materials genome approach to accelerating materials innovation. APL Mater. 1, 011002 (2013). DOI: 10.1063/1.4812323.",
    },
    "pymatgen": {
        "label": "Ong et al., 2013",
        "reference": "Ong, S. P. et al. Python Materials Genomics (pymatgen): a robust, open-source python library for materials analysis. Comput. Mater. Sci. 68, 314-319 (2013). DOI: 10.1016/j.commatsci.2012.10.028.",
    },
    "chgnet": {
        "label": "Deng et al., 2023",
        "reference": "Deng, B. et al. CHGNet as a pretrained universal neural network potential for charge-informed atomistic modelling. Nat. Mach. Intell. 5, 1031-1041 (2023). DOI: 10.1038/s42256-023-00716-3.",
    },
    "neb": {
        "label": "Henkelman et al., 2000",
        "reference": "Henkelman, G.; Uberuaga, B. P.; Jonsson, H. A climbing image nudged elastic band method for finding saddle points and minimum energy paths. J. Chem. Phys. 113, 9901-9904 (2000). DOI: 10.1063/1.1329672.",
    },
    "ehull": {
        "label": "Ong et al., 2008",
        "reference": "Ong, S. P.; Wang, L.; Kang, B.; Ceder, G. Li-Fe-P-O2 phase diagram from first principles calculations. Chem. Mater. 20, 1798-1807 (2008). DOI: 10.1021/cm702327g.",
    },
    # --- Visualization / infrastructure ---
    "multiwfn": {
        "label": "Lu & Chen, 2012",
        "reference": "Lu, T.; Chen, F. Multiwfn: a multifunctional wavefunction analyzer. J. Comput. Chem. 33, 580-592 (2012). DOI: 10.1002/jcc.22885.",
    },
    "vmd": {
        "label": "Humphrey et al., 1996",
        "reference": "Humphrey, W.; Dalke, A.; Schulten, K. VMD: visual molecular dynamics. J. Mol. Graph. 14, 33-38 (1996). DOI: 10.1016/0263-7855(96)00018-5.",
    },
    "aiida": {
        "label": "Huber et al., 2020",
        "reference": "Huber, S. P. et al. AiiDA 1.0, a scalable computational infrastructure for automated reproducible workflows and data provenance. Sci. Data 7, 300 (2020). DOI: 10.1038/s41597-020-00638-4.",
    },
    # --- Cell-level / continuum battery modelling (battery design) ---
    "dfn": {
        "label": "Doyle et al., 1993",
        "reference": "Doyle, M.; Fuller, T. F.; Newman, J. Modeling of galvanostatic charge and discharge of the lithium/polymer/insertion cell. J. Electrochem. Soc. 140, 1526-1533 (1993). DOI: 10.1149/1.2221597.",
    },
    "pybamm": {
        "label": "Sulzer et al., 2021",
        "reference": "Sulzer, V.; Marquis, S. G.; Timms, R.; Robinson, M.; Chapman, S. J. Python Battery Mathematical Modelling (PyBaMM). J. Open Res. Softw. 9, 14 (2021). DOI: 10.5334/jors.309.",
    },
    "comsol": {
        "label": "COMSOL AB, 2023",
        "reference": "COMSOL Multiphysics, Version 6.x. COMSOL AB, Stockholm, Sweden. www.comsol.com.",
    },
    "bruggeman": {
        "label": "Bruggeman, 1935",
        "reference": "Bruggeman, D. A. G. Berechnung verschiedener physikalischer Konstanten von heterogenen Substanzen. Ann. Phys. 416, 636-664 (1935). DOI: 10.1002/andp.19354160705.",
    },
    # --- Polymer electrolyte MD ---
    "moltemplate": {
        "label": "Jewett et al., 2021",
        "reference": "Jewett, A. I. et al. Moltemplate: a tool for coarse-grained modeling of complex biological matter and soft condensed matter physics. J. Mol. Biol. 433, 166841 (2021). DOI: 10.1016/j.jmb.2021.166841.",
    },
}


REFERENCE_ORDER = list(REFERENCE_LIBRARY.keys())


# ---------------------------------------------------------------------------
# Provenance -> citation key resolution (typed, not substring heuristics)
# ---------------------------------------------------------------------------

# Each lookup table maps a *normalized token* to the citation keys it implies.
# Normalization = lower-case, strip, collapse separators.  We match by token /
# prefix against the typed field value, never by scanning a big concatenated
# config string.

_ENGINE_KEYS: Dict[str, List[str]] = {
    "lammps": ["lammps"],
    "gromacs": ["lammps"],          # GROMACS path still cites the MD engine family note below
    "pyscf": ["pyscf", "pyscf2020"],
    "gaussian": ["gaussian"],
    "g16": ["gaussian"],
    "orca": ["orca"],
    "xtb": ["xtb"],
    "qe": ["qe2017", "sssp"],
    "quantumespresso": ["qe2017", "sssp"],
    "pw": ["qe2017", "sssp"],
    "cp2k": ["cp2k", "gth", "molopt"],
}

# GROMACS gets its own real citation rather than reusing LAMMPS'.
REFERENCE_LIBRARY["gromacs"] = {
    "label": "Abraham et al., 2015",
    "reference": "Abraham, M. J. et al. GROMACS: high performance molecular simulations through multi-level parallelism from laptops to supercomputers. SoftwareX 1-2, 19-25 (2015). DOI: 10.1016/j.softx.2015.06.001.",
}
_ENGINE_KEYS["gromacs"] = ["gromacs"]
REFERENCE_ORDER = list(REFERENCE_LIBRARY.keys())

_FUNCTIONAL_BASE_KEYS: Dict[str, List[str]] = {
    "pbe": ["pbe"],
    "pbesol": ["pbe"],
    "pbe0": ["pbe0"],
    "pbeh": ["pbe0"],
    "b3lyp": ["b3lyp"],
    "wb97xd": ["wb97xd"],
    "wb97x": ["wb97xd"],
    "m062x": ["m062x"],
    "m06": ["m062x"],
}

_FORCEFIELD_KEYS: Dict[str, List[str]] = {
    "opls": ["oplsaa"],
    "oplsaa": ["oplsaa"],
    "opls-aa": ["oplsaa"],
    "gaff": ["gaff"],
    "gaff2": ["gaff"],
    "amber": ["amber"],
    "ff14sb": ["amber"],
}

_CHARGE_KEYS: Dict[str, List[str]] = {
    "resp": ["resp"],
    "ligpargen": ["ligpargen"],
    "opls": ["ligpargen"],
    "ecc": ["ecc"],
    "scaled": ["ecc"],
}

_SOLVENT_MODEL_KEYS: Dict[str, List[str]] = {
    "smd": ["smd"],
    "pcm": ["pcm"],
    "cpcm": ["pcm"],
    "iefpcm": ["pcm"],
    "ddcosmo": ["pcm"],
    "cosmo": ["pcm"],
}


def _normalize(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _tokens(value: Optional[str]) -> List[str]:
    if not value:
        return []
    raw = str(value).lower()
    out: List[str] = []
    token = []
    for ch in raw:
        if ch.isalnum():
            token.append(ch)
        else:
            if token:
                out.append("".join(token))
                token = []
    if token:
        out.append("".join(token))
    return out


def functional_citation_keys(functional: Optional[str]) -> List[str]:
    """Resolve a DFT functional string (e.g. 'PBE-D3(BJ)', 'wB97X-D', 'B3LYP')
    to citation keys, handling the dispersion suffix separately from the base
    functional.  Crucially, 'B3LYP' never resolves to 'pbe'."""
    if not functional:
        return []
    norm = _normalize(functional)
    keys: List[str] = []
    # Grimme dispersion correction (check BJ damping before plain D3).
    # NOTE: wB97X-D uses its own built-in (-D2 type) dispersion, NOT Grimme D3,
    # so 'wb97xd' must not pick up a 'd3' key here.
    if "d3bj" in norm or "d3(bj)" in functional.lower().replace(" ", ""):
        keys.append("d3bj")
    elif "d3" in norm:
        keys.append("d3")
    # base functional: strip dispersion markers then match against known bases
    base = norm
    for marker in ("d3bj", "d3", "dftd3", "gd3bj", "gd3"):
        base = base.replace(marker, "")
    matched_base = None
    for token, ks in _FUNCTIONAL_BASE_KEYS.items():
        if base == token or base.startswith(token):
            matched_base = ks
            break
    if matched_base:
        for k in matched_base:
            if k not in keys:
                keys.insert(0, k)
    return keys


def _lookup_any(value: Optional[str], table: Dict[str, List[str]]) -> List[str]:
    """Token/prefix match a typed field against a lookup table."""
    keys: List[str] = []
    norm = _normalize(value)
    if not norm:
        return keys
    for token, ks in table.items():
        tnorm = _normalize(token)
        if not tnorm:
            continue
        if norm == tnorm or norm.startswith(tnorm) or tnorm in _tokens(value):
            for k in ks:
                if k not in keys:
                    keys.append(k)
    return keys


def resolve_provenance_citations(
    *,
    engine: Optional[str] = None,
    functional: Optional[str] = None,
    basis: Optional[str] = None,
    forcefield: Optional[str] = None,
    charge_method: Optional[str] = None,
    solvent_model: Optional[str] = None,
    pseudopotentials: Optional[str] = None,
    extra: Optional[Iterable[str]] = None,
) -> List[str]:
    """Map a job's *typed* provenance fields to an ordered, unique key list."""
    keys: List[str] = []

    def _add(more: Iterable[str]) -> None:
        for k in more:
            if k in REFERENCE_LIBRARY and k not in keys:
                keys.append(k)

    _add(_lookup_any(engine, _ENGINE_KEYS))
    _add(functional_citation_keys(functional))
    if basis and "def2" in _normalize(basis):
        _add(["def2"])
    _add(_lookup_any(forcefield, _FORCEFIELD_KEYS))
    _add(_lookup_any(charge_method, _CHARGE_KEYS))
    _add(_lookup_any(solvent_model, _SOLVENT_MODEL_KEYS))
    if pseudopotentials:
        pnorm = _normalize(pseudopotentials)
        if "gth" in pnorm:
            _add(["gth"])
        if "sssp" in pnorm or "pslibrary" in pnorm or "ultrasoft" in pnorm:
            _add(["sssp"])
    if extra:
        _add([k for k in extra if isinstance(k, str)])
    return keys


# Baseline citations per module kind — guarantees a sensible reference list
# even when a specific functional/forcefield cannot be resolved.
MODULE_DEFAULT_CITATIONS: Dict[str, List[str]] = {
    "md": ["lammps", "oplsaa", "einstein_msd", "nernst_einstein"],
    "qc": ["pyscf"],
    "cluster": ["pyscf"],
    "electrode_aimd": ["cp2k", "aimd", "gth", "molopt"],
    "electrode_md": ["lammps", "einstein_msd"],
    "surface_adsorption": ["qe2017", "sssp"],
    "interaction": ["pyscf"],
    "sse": ["materialsproject", "pymatgen", "chgnet"],
    "reaction_network": ["pyscf"],
    "polymer": ["lammps", "gaff"],
}


# ---------------------------------------------------------------------------
# Numbered reference list (by first appearance)
# ---------------------------------------------------------------------------

def build_reference_map(keys: Iterable[str], *, allowed_keys: Optional[Iterable[str]] = None) -> Dict[str, int]:
    """Assign [1], [2], ... by first appearance; unknown keys are skipped."""
    allowed = set(allowed_keys) if allowed_keys is not None else REFERENCE_LIBRARY
    ref_map: Dict[str, int] = {}
    n = 0
    for key in keys:
        if key in allowed and key not in ref_map:
            n += 1
            ref_map[key] = n
    return ref_map


def reference_entries(ref_map: Dict[str, int]) -> List[Dict[str, object]]:
    """Numbered reference rows sorted by number, for the References section."""
    rows = []
    for key, number in sorted(ref_map.items(), key=lambda kv: kv[1]):
        entry = REFERENCE_LIBRARY[key]
        rows.append({
            "number": number,
            "key": key,
            "label": entry["label"],
            "reference": entry["reference"],
        })
    return rows


def inline_marker(key: str, ref_map: Dict[str, int]) -> str:
    """Render a citation key as its numeric in-text marker, e.g. '[3]'."""
    num = ref_map.get(key)
    return f"[{num}]" if num else ""


# ---------------------------------------------------------------------------
# Back-compat shims (deprecated — kept so old call sites do not crash during
# the migration; new code should use the numbered API above)
# ---------------------------------------------------------------------------

def cite(*keys: str) -> str:  # pragma: no cover - deprecated
    labels = [REFERENCE_LIBRARY[key]["label"] for key in keys if key in REFERENCE_LIBRARY]
    return f"（{'; '.join(labels)}）" if labels else ""


def unique_reference_keys(keys: Iterable[str]) -> List[str]:
    present = {key for key in keys if key in REFERENCE_LIBRARY}
    ordered = [key for key in REFERENCE_ORDER if key in present]
    ordered.extend([key for key in present if key not in ordered])
    return ordered


def reference_rows(keys: Iterable[str]) -> List[Dict[str, str]]:  # pragma: no cover
    return [
        {"key": key, "label": REFERENCE_LIBRARY[key]["label"], "reference": REFERENCE_LIBRARY[key]["reference"]}
        for key in unique_reference_keys(keys)
    ]
