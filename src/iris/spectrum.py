'''
--------------------------------------------------------------
IRIS: (GPU-accelerated) IR spectrum modeling

Evaluation of optical depths and intensities is jit-enabled and
vectorized. 
--------------------------------------------------------------
Developed by Carlos E. Romero-Mirza (2024)
'''
import jax
jax.config.update("jax_enable_x64", True)

import astropy.units as u
#from astropy.constants import c,h, k_B, G, M_sun, au, pc, u # added by Emma. add into const later
import astropy.constants as const
import jax.numpy as jnp
import numpy as np
from functools import partial
from jax.scipy.signal import fftconvolve

# -------------------- Global constants ----------------------
# for more efficient unit handling
# au to cm
aucm = const.au.to(u.cm)
# pc to cm
pccm = const.pc.to(u.cm)
# squared arcsec to sr
arcsec2_to_sr = ((u.arcsec**2).to(u.sr))
# c in micron/sec
c_musec = (const.c.to(u.micron/u.s)).value
# c in cm/sec
c_cmsec = (const.c.cgs).value
# km/s to micron/s
kms_to_mus = ((1*u.km/u.s).to(u.micron/u.s)).value
# plack const. in cgs
h_cgs = (const.h.cgs).value
# boltzmann const. in cgs
k_B_cgs = (const.k_B.cgs).value    
# c/micron
micron_to_hz = (const.c/(1*u.micron)).to(u.Hz)
# mass of the proton
mp_cgs = const.m_p.cgs.value

au_to_m = u.au.to(u.m)
M_sun_to_kg = (u.M_sun).to(u.kg)
# au^2 to m^2
sq_au_to_sq_m = (u.au.to(u.m))**2 

# ----------------------------------------------------------

# --------------- Main modeling code -----------------------

def J_profile(wavelength, t_ex): 
    """
    J_profile: full (blackbody) Plack function

    :wavelength: wavelength in micron
    :t_ex: temperature in K
    
    :return: blackbody flux density in erg cm-2 s-1 Hz-1 sr-1
    """ 
    nu = c_cmsec/(wavelength*1e-4)
    return (2*h_cgs*nu**3)/(c_cmsec**2) * (jnp.exp(h_cgs*nu/(k_B_cgs*t_ex)) - 1)**(-1)
    
J_profile = jax.jit(J_profile)

def dv_to_dlam(dv, lam_rest):
    """
    dv_to_dlam: convert velocity element in km/s to wavelength element in micron,
                given a reference wavelength
    :dv: elocity element in km/s
    :lam_rest: reference wavelength in micron
    
    returns: dlam in micron
    """
    nu_rest = c_musec/lam_rest
    lam =  -c_musec / (((dv*kms_to_mus*nu_rest)/c_musec) - nu_rest)
    return lam - lam_rest
dv_to_dlam = jax.jit(dv_to_dlam)

def keplerian_velocity(r, M_star):
    """
    Calculate the Keplerian velocity at radius r for a star of mass M_star.
    """
    G = 6.67430e-11  # Gravitational constant in m^3 kg^-1 s^-2
    return (G * M_star / r)**0.5
keplerian_velocity = jax.jit(keplerian_velocity)

def line_of_sight_velocity(r, phi, inc, M_star):
    """
    Calculate the line-of-sight velocity for a given radius r, azimuthal angle phi,
    inclination i, and stellar mass M_star.
    """
    v_k = keplerian_velocity(r, M_star)
    return v_k * jnp.sin(inc) * jnp.cos(phi)
line_of_sight_velocity = jax.jit(line_of_sight_velocity)

def keplerian_broadening_kernel(r_in, r_out, M_star, inc, w_ref=14.0, dw_ref=6e-5):
    """
    Construct the Keplerian broadening kernel for a disk with inner radius r_in,
    outer radius r_out, stellar mass M_star, and inclination i.
    """
    dr = 0.001 * au_to_m
    dphi = 2*jnp.pi/10000

    r_in = r_in * au_to_m
    r_out = r_out * au_to_m

    M_star = M_star * M_sun_to_kg

    r = jnp.linspace(0.001, 50.1, 10000) * au_to_m
    r = jnp.where(r<r_in, 0, r )
    r = jnp.where(r>r_out, 0, r )

    phi = jnp.arange(0, 2 * jnp.pi+dphi, dphi)
    r_mesh, phi_mesh = jnp.meshgrid(r, phi)

    # Calculate line-of-sight velocities
    v_los = line_of_sight_velocity(r_mesh, phi_mesh, inc, M_star)
    # Calculate wavelengths
    lam_los = dv_to_dlam(v_los/1e3, w_ref)
    # Calculate pixels
    pix_los = lam_los/dw_ref

    # Flatten the pixel array and create a histogram
    pix_los_flat = pix_los.flatten()
    hist, bin_edges = jnp.histogram(pix_los_flat, bins=np.arange(-500 , 500, 1), density=True)

    # Calculate bin centers
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

    return bin_centers, hist
keplerian_broadening_kernel = jax.jit(keplerian_broadening_kernel)

def evaluate_line_tau(fine_wgrid, sigma_lam, tau_cen, line_w):
    """
    evaluate_line_tau: get line optical depth profile 
    
    :fine_wgrid: fine wavelength grid to evaluate tau 
    :sigma_lam: intrinsic line (gaussian) width in micron
    :tau_cen: optical depth at line center
    :line_w: wavelenbgth at line center in micron
    
    returns: tau (unitless)
    """
    # Gaussian profile
    return tau_cen * jnp.exp(-((fine_wgrid-line_w)**2)/(2*sigma_lam**2) )
evaluate_line_tau = jax.jit(evaluate_line_tau)

def compute_tau_grid(line_catalog, fine_wgrid, dv, t_ex, n_mol):
    """
    compute_tau_grid: get optical depth profile for all lines in catalog
    
    :line_catalog: HITRAN catalog 
    :fine_wgrid: fine wavelength grid to evaluate tau 
    :dv: intrinsic line FWHM in km/s
    :t_ex: excitation temperature in K
    :n_mol: column density in cm^-2
    
    returns: tau (unitless)
    """
    # partition function
    q_sum = jnp.interp(t_ex, line_catalog['Qt'], line_catalog['Qv'])
    # population levels
    x_low = line_catalog['glow'] * jnp.exp(-line_catalog['elow'] / t_ex) / q_sum
    x_up = line_catalog['gups'] * jnp.exp(-line_catalog['eups'] / t_ex) / q_sum
    # optical depth
    tau_amp = (jnp.log(2) / jnp.pi)**0.5 * (line_catalog['aijs'] * n_mol * (line_catalog['ws']*1e-4)**3) / (4*jnp.pi*dv*(1e5))
    tau_lvl = x_low * line_catalog['gups']/line_catalog['glow'] - x_up
    tau_cen = tau_amp * tau_lvl
    # intrinsic width in micron
    sigma_lam = dv_to_dlam(dv/2.355, fine_wgrid)
    # map over all lines
    func = partial(evaluate_line_tau, fine_wgrid, sigma_lam)
    tau_grid = jax.vmap(func)(tau_cen, line_catalog['ws'])
    
    # sum opacities
    return  jnp.sum(tau_grid, axis=0) # sum taus along rows
compute_tau_grid = jax.jit(compute_tau_grid)


def compute_tau_grid_lineflux(line_catalog, fine_wgrid, dv, t_ex, n_mol):
    """
    copied from compute_tau_grid().
    compute_tau_grid_lineflux: get optical depth profile for a given line; do not sum at the end
    Added by Emma 3/12/25
    
    :line_catalog: HITRAN catalog 
    :fine_wgrid: fine wavelength grid to evaluate tau 
    :dv: intrinsic line FWHM in km/s
    :t_ex: excitation temperature in K
    :n_mol: column density in cm^-2
    
    returns: tau (unitless)
    """
    # partition function
    q_sum = jnp.interp(t_ex, line_catalog['Qt'], line_catalog['Qv'])
    # population levels
    x_low = line_catalog['glow'] * jnp.exp(-line_catalog['elow'] / t_ex) / q_sum
    x_up = line_catalog['gups'] * jnp.exp(-line_catalog['eups'] / t_ex) / q_sum
    # optical depth
    tau_amp = (jnp.log(2) / jnp.pi)**0.5 * (line_catalog['aijs'] * n_mol * (line_catalog['ws']*1e-4)**3) / (4*jnp.pi*dv*(1e5))
    tau_lvl = x_low * line_catalog['gups']/line_catalog['glow'] - x_up
    tau_cen = tau_amp * tau_lvl
    # intrinsic width in micron
    sigma_lam = dv_to_dlam(dv/2.355, fine_wgrid)
    # pull center wavenumber 
    center_wn = 1e4/line_catalog['ws'] # central wavenumber
    # map over all lines
    func = partial(evaluate_line_tau, fine_wgrid, sigma_lam)
    tau_grid = jax.vmap(func)(tau_cen, line_catalog['ws'])
    
    # DON'T sum opacities
    return  jnp.array(tau_grid) # 2D (?) array w/ 1 row for each line for a given species, columns = fine_wgrid
compute_tau_grid_lineflux = jax.jit(compute_tau_grid_lineflux)

def central_wavenumber(line_catalog):
    """
    returns central wavenumber of lines.
    
    :line_catalog: HITRAN catalog 
    
    returns: center_wn, wavenumber in * m^-1 * at center of given line. wn0 from slabspec.
    """
    center_wn = 1e6/line_catalog['ws'] # wavenumber
    return center_wn # 
central_wavenumber = jax.jit(central_wavenumber)
    


def compute_total_tau(catalog, fine_wgrid, dv, t_ex, n_mol):
    """
    compute_tau_grid: get optical depth profile for all molecules
    
    :catalog: HITRAN catalogs
    :fine_wgrid: fine wavelength grid to evaluate tau 
    :dv: intrinsic line FWHM in km/s
    :t_ex: excitation temperature in K
    :n_mol: column density in cm^-2
    
    returns: tau (unitless)
    """
    keys = list(catalog.keys())
    total_tau = []
    # loop over each species
    for i in range(len(catalog)): # calculate the total optical depth for each molecule using compute_tau_grid()
        # map over disk grid
        func = partial(compute_tau_grid, catalog[keys[i]], fine_wgrid)
        total_tau.append(jax.vmap(func)(dv[i], t_ex[i], n_mol[i]))
    return jnp.array(total_tau)

compute_total_tau = jax.jit(compute_total_tau)


''' # I don't think we need to do this for line fluxes...? 
def compute_total_tau_lineflux(catalog, fine_wgrid, dv, t_ex, n_mol):
    """
    compute_tau_grid: get optical depth profile for all molecules
    different than compute_total_tau bc it calls compute_tau_grid_lineflux, which does not sum the taus over a line
    
    :catalog: HITRAN catalogs
    :fine_wgrid: fine wavelength grid to evaluate tau 
    :dv: intrinsic line FWHM in km/s
    :t_ex: excitation temperature in K
    :n_mol: column density in cm^-2
    
    returns: array of tau profiles for each line. each row will be as large as fine_wgrid
    """
    keys = list(catalog.keys())
    total_tau_lineflux = jnp.zeros((len(catalog),len(fine_wgrid)))
    # loop over each species
    for i in range(len(catalog)):
        # map over disk grid
        func = partial(compute_tau_grid_lineflux, catalog[keys[i]], fine_wgrid)
        total_tau_lineflux[i,:] = (jax.vmap(func)(dv[i], t_ex[i], n_mol[i]))
    return jnp.array(total_tau_lineflux)
compute_total_tau_lineflux = jax.jit(compute_total_tau_lineflux)'''



def compute_fdens(distance, fine_wgrid, total_tau, A_au, t_ex):
    """
    compute_fdens: get flux density for one species
    
    :distance: distance to source in pc
    :fine_wgrid: fine wavelength grid to evaluate tau 
    :total_tau: tau as function of wavelength
    :A_au: emitting area in au^2
    :t_ex: excitation temperature in K
    
    returns: flux density (erg cm-2 s-1 Hz-1)
    """
    # radius assuming circular area
    eq_radius = (A_au/jnp.pi)**0.5
    # line intensities accounting for saturation at line center
    return jnp.pi * (eq_radius/distance)**2 * ((aucm / pccm) ** 2) * J_profile(fine_wgrid, t_ex) * (1 - jnp.exp(-total_tau))
compute_fdens = jax.jit(compute_fdens)



def compute_lineflux(catalog, distance, A_au, t_ex, n_mol, dv, fine_wgrid):
    '''
    compute_lineflux: get line flux for the species within catalog. 
    Uses parts of compute_total_tau() to calculate flux here for each line

    :catalog: HITRAN catalogs
    :distance: distance to source in pc
    :A_au: emitting area in au^2
    :t_ex: excitation temperature in K
    :n_mol: column density in cm^-2
    :dv: intrinsic line FWHM in km/s
    :fine_wgrid: fine wavelength grid to evaluate tau 
    
    returns: integrated line flux (W m^-2) for each line within user-defined wavelength range
    '''
    # !! add stuff here
    keys = list(catalog.keys())
    
    # make empty flux array that is number of lines by number of wavelengths. For each line, will insert flux
    #f_arr = jnp.zeros((len(catalog), len(fine_wgrid))) # commented out bc I think I should calculate a single f_arr for each species?
    
    lineflux = jnp.zeros((len(catalog),len(fine_wgrid))) # lineflux list for each species?
    #func = partial(compute_tau_grid_lineflux, catalog, fine_wgrid)
    #tau = jax.vmap(func)(dv, t_ex, n_mol) # grid of optical depth profiles

    #tau = compute_tau_grid_lineflux(catalog[keys], fine_wgrid, dv, t_ex, n_mol) # grid of optical depth profiles
    
    #return jnp.array(tau)
    
    # loop over each species
    for i in range(len(catalog)): 
        #loop over lines?
        #for j in range(len(catalog[keys[i]]['ws'])): 
    
        # map over disk grid
        # find optical depth as a function of wavelength for that line (??? how to call function here)

        # calculate the wavelength-dependent tau profiles for each line for a given molecule
        func = partial(compute_tau_grid_lineflux, catalog[keys[i]], fine_wgrid)
        tau = jax.vmap(func)(dv[i], t_ex[i], n_mol[i]) # !! This appears to give me a tau of 0

        # the follwing code/comments is based off of slabspec.py ~lines 276-278 and relevant variables
        # wno -> wavenumber (m^-1) at center of line
        # wnfactor=const.h.value*const.c.value*wn0/(const.k_B.value*t_ex[i])
        # si2jy=1e26   #SI to Jy flux conversion factor
        # omega=area/(d_pc*const.pc.value)**2. # area is in m^2, so use A_au and convert to m^2 from au^2 # !! make sure pc.value works
        
        wn0 = central_wavenumber(catalog[keys[i]]) # central wavenumbers for all lines
        w0 = 1.0e6/wn0
        wnfactor=const.h.value*const.c.value*wn0/(const.k_B.value*t_ex[i])
        si2jy=1e26   #SI to Jy flux conversion factor
        area=A_au*sq_au_to_sq_m # convert area in AU^2 to m^2
        omega=area/(distance*const.pc.value)**2. 

        #f_arr[i,:]=2*const.h.value*const.c.value*wn0**3./(jnp.exp(wnfactor)-1.0e0)*(1-jnp.exp(-tau))*si2jy*omega
        f_arr=2*const.h.value*const.c.value*wn0**3./(jnp.exp(wnfactor)-1.0e0)*(1-jnp.exp(-tau))*si2jy*omega
        # f_arr will hopefully be a 2D array of flux profiles for each line for a given molecule? likely will have an issue here w/ wn0 and tau being different shapes

        lineflux_jykms=jnp.sum(f_arr, axis=0)*dv # sum those fluxes for each line
        # dvel=0.1e0    #km/s # constant in Colette's code
        # using dv instead, in km/sec already
        
        lineflux[i,:]=lineflux_jykms*1e-26*1.*1e5*(1./(w0*1e-4))    #mks
        #w0=1.e6/wn0
    
    return jnp.array(lineflux) # hopefully the integrated line fluxes at each wavelength within obs_wgrid
compute_lineflux = jax.jit(compute_lineflux)

def compute_fdens_keplerian(distance, fine_wgrid, total_tau, A_au, t_ex, r_in, M_star, inc):
    """
    compute_fdens: get flux density for one species, including keplerian profile
    
    :distance: distance to source in pc
    :fine_wgrid: fine wavelength grid to evaluate tau 
    :total_tau: tau as function of wavelength
    :A_au: emitting area in au^2
    :t_ex: excitation temperature in K
    :r_in: innermost radii in au
    :M_star: stellar mass in solar mass
    :inc: inclination in radian

    returns: flux density (erg cm-2 s-1 Hz-1)
    """
    fdens = compute_fdens(distance, fine_wgrid, total_tau, A_au, t_ex)
    w_ref = jnp.median(fine_wgrid)
    dw_ref = jnp.max(jnp.gradient(fine_wgrid))

    r_out = (A_au/np.pi + r_in**2)**0.5
    keplerian_pix, keplerian_kernel = keplerian_broadening_kernel(r_in, r_out, M_star, inc, w_ref, dw_ref)
    fdens = fftconvolve(fdens, keplerian_kernel, mode='same')

    return fdens 
compute_fdens_keplerian = jax.jit(compute_fdens_keplerian)

def compute_total_fdens(catalog, distance, A_au, t_ex, n_mol, dv, fine_wgrid):
    """
    compute_total_fdens: get total flux density in Jy
    
    :catalog: HITRAN catalog (dict)
    :distance: distance to source in pc
    :A_au: emitting area in au^2
    :t_ex: excitation temperature in K
    :n_mol: column density in cm^-2
    :dv: intrinsic line FWHM in km/s
    :fine_wgrid: fine wavelength grid to evaluate tau 
   
    returns: flux density (Jy)
    """
    fdens = fine_wgrid*0.0
    # evaluate total optical depth profile
    total_tau = compute_total_tau(catalog, fine_wgrid, dv, t_ex, n_mol)
    # loop over species
    for i in range(len(catalog)):
        # map over disk grid
        func = partial(compute_fdens, distance, fine_wgrid)
        fdens_i = jax.vmap(func)(total_tau[i], A_au[i], t_ex[i])
        fdens += jnp.sum(fdens_i, axis=0) 
    return fdens * 1e23
compute_total_fdens = jax.jit(compute_total_fdens)

def compute_total_fdens_keplerian(catalog, distance, A_au, t_ex, n_mol, dv, r_in, M_star, inc, fine_wgrid):
    """
    compute_total_fdens_keplerian: get total flux density in Jy including Keplerian profile
    
    :catalog: HITRAN catalog (dict)
    :distance: distance to source in pc
    :A_au: emitting area in au^2
    :t_ex: excitation temperature in K
    :n_mol: column density in cm^-2
    :dv: intrinsic line FWHM in km/s
    :r_in: innermost radii in au
    :M_star: stellar mass in solar mass
    :inc: inclination in radian
    :fine_wgrid: fine wavelength grid to evaluate tau 
   
    returns: flux density (Jy)
    """
    fdens = fine_wgrid*0.0
    # evaluate total optical depth profile
    total_tau = compute_total_tau(catalog, fine_wgrid, dv, t_ex, n_mol)
    # loop over species
    for i in range(len(catalog)):
        # map over disk grid 
        func = partial(compute_fdens_keplerian, distance, fine_wgrid)
        fdens_i = jax.vmap(func)(total_tau[i], A_au[i], t_ex[i], r_in[i], M_star[i], inc[i])
        fdens += jnp.sum(fdens_i, axis=0) 
    return fdens * 1e23
compute_total_fdens_keplerian = jax.jit(compute_total_fdens_keplerian)
