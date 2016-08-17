import numpy as np
import healpy as hp
import os
from scipy import interpolate
import ionRIME_funcs as irf
import sys
import time
import numba_funcs as irnf
import radiono

def Hz2GHz(freq):
    return freq / 1e9

def get_gsm_cube():
    sys.path.append('/data4/paper/zionos/polskysim')
    import gsm2016_mod
    import astropy.coordinates as coord
    import astropy.units as units

    nside_in = 64
    npix_in = hp.nside2npix(nside_in)
    I_gal = np.zeros((p.nfreq, npix_in))
    for fi, f in enumerate(p.nu_axis):
        I_gal[fi] = gsm2016_mod.get_gsm_map_lowres(Hz2GHz(f))

    x_c = np.array([1.,0,0]) # unit vectors to be transformed by astropy
    y_c = np.array([0,1.,0])
    z_c = np.array([0,0,1.])

    # The GSM is given in galactic coordinates. We will rotate it to J2000 equatorial coordinates.
    axes_icrs = coord.SkyCoord(x=x_c, y=y_c, z=z_c, frame='icrs', representation='cartesian')
    axes_gal = axes_icrs.transform_to('galactic')
    axes_gal.representation = 'cartesian'

    R = np.array(axes_gal.cartesian.xyz) # The 3D rotation matrix that defines the coordinate transformation.

    npix_out = hp.nside2npix(p.nside)
    I = np.zeros((p.nfreq, npix_out))

    for i in range(p.nfreq):
        I[i] = irf.rotate_healpix_map(I_gal[i], R)
        I[i] = irf.harmonic_ud_grade(I[i], nside_in, p.nside)

    return I
def get_cora_polsky(pfrac_max=None):
    from cora.foreground import galaxy

    gal = galaxy.ConstrainedGalaxy()
    gal.nside = p.nside
    gal.frequencies = p.nu_axis

    stokes_cubes = gal.getpolsky()
    I,Q,U,V = [stokes_cubes[:,i,:] for i in range(4)]

    return I,Q,U,V

def transform_basis(nside, jones, z0_cza, R_z0):
    """
    At zenith in the local frame the 'x' feed is aligned with 'theta' and
    the 'y' feed is aligned with 'phi'
    """
    npix = hp.nside2npix(nside)
    hpxidx = np.arange(npix)
    cza, ra = hp.pix2ang(nside, hpxidx)

    # Rb is the rotation relating the E-field basis coordinate frame to the local horizontal zenith.
    # (specific to this instrument response simulation data)
    Rb = np.array([
    [0,0,-1],
    [0,-1,0],
    [-1,0,0]
    ])

    fR = np.einsum('ab,bc->ac', Rb, R_z0) # matrix product of two rotations

    tb, pb = irf.rotate_sphr_coords(fR, cza, ra)

    cza_v = irf.t_hat_cart(cza, ra)
    ra_v = irf.p_hat_cart(cza, ra)

    tb_v = irf.t_hat_cart(tb, pb)

    fRcza_v = np.einsum('ab...,b...->a...', fR, cza_v)
    fRra_v = np.einsum('ab...,b...->a...', fR, ra_v)

    cosX = np.einsum('a...,a...', fRcza_v, tb_v)
    sinX = np.einsum('a...,a...', fRra_v, tb_v)

    basis_rot = np.array([[cosX, sinX],[-sinX, cosX]])
    basis_rot = np.transpose(basis_rot,(2,0,1))

    return irnf.M(jones, basis_rot)

def instrument_setup(z0_cza, freqs):
    """
    This is the CST simulation using the efield basis of z' = -x, y' = -y, x' = -z
    frequencies are every 10MHz, from 100-200
    Each file contains 8 columns which are ordered as:
          (Re(xt),Re(xp),Re(yt),Re(yp),Im(xt),Im(xp),Im(yt),Im(yp)).
    Each column is a healpix map with resolution nside = 2**8
    """

    nu0 = str(int(p.nu_axis[0] / 1e6))
    nuf = str(int(p.nu_axis[-1] / 1e6))
    band_str = nu0 + "-" + nuf

    # restore_name = p.interp_type + "_" + "band_" + band_str + "mhz_nfreq" + str(p.nfreq)+ "_nside" + str(p.nside) + ".npy"
    #
    # if os.path.exists('jones_save/' + restore_name) == True:
    #     return np.load('jones_save/' + restore_name)
    #
    local_jones0_file = 'local_jones0/nside' + str(p.nside) + '_band' + band_str + '_Jdata.npy'

    if os.path.exists(local_jones0_file) == True:
        return np.load(local_jones0_file)

    fbase = '/data4/paper/zionos/HERA_jones_data/HERA_Jones_healpix_'
    # fbase = '/home/zmart/radcos/polskysim/IonRIME/HERA_jones_data/HERA_Jones_healpix_'

    nside_in = 2**8
    fnames = [fbase + str(int(f / 1e6)) + 'MHz.txt' for f in freqs]
    nfreq_nodes = len(freqs)

    npix = hp.nside2npix(nside_in)
    hpxidx = np.arange(npix)
    cza, ra = hp.pix2ang(nside_in, hpxidx)

    z0 = irf.r_hat_cart(z0_cza, 0.)

    RotAxis = np.cross(z0, np.array([0,0,1.]))
    RotAxis /= np.sqrt(np.dot(RotAxis,RotAxis))
    RotAngle = np.arccos(np.dot(z0, [0,0,1.]))

    R_z0 = irf.rotation_matrix(RotAxis, RotAngle)

    t0, p0 = irf.rotate_sphr_coords(R_z0, cza, ra)

    hm = np.zeros(npix)
    hm[np.where(cza < (np.pi / 2. + np.pi / 20.))] = 1 # Horizon mask; is 0 below the local horizon.
    # added some padding. Idea being to allow for some interpolation near the horizon. Questionable.
    npix_out = hp.nside2npix(p.nside)

    Jdata = np.zeros((nfreq_nodes,npix_out,2,2),dtype='complex128')
    for i,f in enumerate(fnames):
        J_f = np.loadtxt(f) # J_f.shape = (npix_in, 8)

        J_f = J_f * np.tile(hm, 8).reshape(8, npix).transpose(1,0) # Apply horizon mask

        # Could future "rotation" of these zeroed-maps have small errors at the
        # edges of the horizon? due to the way healpy interpolates.
        # Unlikely to be important.
        # Comment update: Yep, it turns out this happens, BUT it is approximately
        # power-preserving. The pixels at the edges of the rotated mask are not
        # identically 1, but the sum over the mask is maintained to about a part
        # in 1e-5

        # Perform a scalar rotation of each component so that the instrument's boresight
        # is pointed toward (z0_cza, 0), the location of the instrument on the
        # earth in the Current-Epoch-RA/Dec coordinate frame.
        J_f = irf.rotate_jones(J_f, R_z0, multiway=False)

        if p.nside != nside_in:
            # Change the map resolution as needed.

            #d = lambda m: hp.ud_grade(m, nside=p.nside, power=-2.)
                # I think these two ended up being (roughly) the same?
                # The apparent normalization problem was really becuase of an freq. interpolation problem.
                # irf.harmonic_ud_grade is probably better for increasing resolution, but hp.ud_grade is
                # faster because it's just averaging/tiling instead of doing SHT's
            d = lambda m: irf.harmonic_ud_grade(m, nside_in, p.nside)
            J_f = (np.asarray(map(d, J_f.T))).T
                # The inner transpose is so that correct dimension is map()'ed over,
                # and then the outer transpose returns the array to its original shape.

        J_f = irf.inverse_flatten_jones(J_f) # Change shape to (nfreq,npix,2,2), complex-valued
        J_f = transform_basis(p.nside, J_f, z0_cza, R_z0) # right-multiply by the basis transformation matrix from RA/Dec to the Local CST basis.
        Jdata[i,:,:,:] = J_f
        print i

    # If the model at the current nside hasn't been generated before, save it for future reuse.
    if os.path.exists(local_jones0_file) == False:
        np.save(local_jones0_file, Jdata)

    return Jdata

def _interpolate_jones_freq(J_in, freqs, multiway=True, interp_type='spline'):
    """
    A scheme to interpolate the spherical harmonic components of jones matrix elements.
    Does not seem to work well, and is unused.
    """
    nfreq_in = len(freqs)

    if multiway == True:
        J_flat = np.zeros((nfreq_in, npix, 8), dtype='float64')
        for i in range(nfreq_in):
            J_flat[i] = irf.flatten_jones(J_in[i])
        J_in = J_flat

    lmax = 3 * nside -1
    nlm = hp.Alm.getsize(lmax)
    Jlm_in = np.zeros(nfreq_in, nlm, 8)
    for i in range(nfreq_in):
        sht = lambda m: hp.map2alm(m, lmax=lmax)
        Jlm_in[i,:,:] = (np.asarray(map(sht, J_in.T))).T

    Jlm_out = np.zeros(p.nfreq, nlm, 8)
    for lm in range(nlm):
        for j in range(8):
            Jlmj_re = np.real(Jlm_in[:,lm,j])
            Jlmj_im = np.imag(Jlm_in[:,lm,j])

            a = interpolate_pixel(Jlmj_re, freqs, p.nu_axis, interp_type=p.interp_type) # note! interpolate_pixel function no longer exists
            b = interpolate_pixel(Jlmj_im, freqs, p.nu_axis, interp_type=p.interp_type)
            Jlm_out[:, lm, j] = a + 1j*b

    # J_in.shape = (p.nfreq_in, ??, 8)

    # Now, return alm's? or spatial maps?

def interpolate_jones_freq(J_in, freqs, multiway=True, interp_type='cubic', save=False):
    #nfreq_out = len(nu_axis)
    nfreq_in = len(freqs)
    npix = len(J_in[0,:,0])
    #nside = hp.npix2nside(npix)

    if multiway == True:
        J_flat = np.zeros((nfreq_in, npix, 8), dtype='float64')
        for i in range(nfreq_in):
            J_flat[i] = irf.flatten_jones(J_in[i])
        J_in = J_flat

    # J_in.shape = (nfreq_in,npix, 8)

    interpolant = interpolate.interp1d(freqs, J_in, kind=interp_type,axis=0)
    J_out = interpolant(p.nu_axis)

    if multiway == True:
        J_m = np.zeros((p.nfreq, npix, 2,2), dtype='complex128')
        for i in range(p.nfreq):
            J_m[i] = irf.inverse_flatten_jones(J_out[i])
        J_out = J_m

    for i in range(p.nfreq):
        Bx_max = np.amax(np.absolute(J_out[i,:,0,0])**2. + np.absolute(J_out[i,:,0,1])**2.)
        By_max = np.amax(np.absolute(J_out[i,:,1,0])**2. + np.absolute(J_out[i,:,1,1])**2.)
        J_out[i,:,0,0] /= np.sqrt(Bx_max)
        J_out[i,:,0,1] /= np.sqrt(Bx_max)
        J_out[i,:,1,0] /= np.sqrt(By_max)
        J_out[i,:,1,1] /= np.sqrt(By_max)

    # Bah, figure it out later
    # Bx_max = np.amax(
    #     np.absolute(J_out[:,:,0,0])**2. + np.absolute(J_out[:,:,0,1])**2.,
    #     axis=1)
    # By_max = np.amax(
    #     np.absolute(J_out[:,:,1,0])**2. + np.absolute(J_out[:,:,1,1])**2.,
    #     axis=1)
    # # Bx_max.shape = By_max = (nfreq,)
    #
    # J_out[:,:,0,:] /= Bx_max[:,None,None]
    #
    # J_out[:,:,1,:] /= By_max[:,None,None]

    if save == True:
        nu0 = str(int(p.nu_axis[0] / 1e6))
        nuf = str(int(p.nu_axis[-1] / 1e6))
        fname = p.interp_type + "_" + "band_" + nu0 + "-" + nuf + "mhz_nfreq" + str(p.nfreq)+ "_nside" + str(p.nside) + ".npy"
        if p.PAPER_instrument == True:
            np.save('jones_save/PAPER/' + fname, J_out)
        else:
            np.save('jones_save/' + fname, J_out)
    return J_out

def map2alm(marr, lmax):
    """
    Vectorized hp.map2alm
    """
    return np.apply_along_axis(lambda m: hp.map2alm(m, lmax=lmax),1,marr)

def alm2map(almarr, nside):
    """
    Vectorized hp.alm2map
    """
    return np.apply_along_axis(lambda alm: hp.alm2map(alm, nside, verbose=False), 1, almarr)

def main(p, save=False):

    npix = hp.nside2npix(p.nside)
    hpxidx = np.arange(npix)
    cza, ra = hp.pix2ang(p.nside, hpxidx)
    l,m = hp.Alm.getlm(p.lmax)

    z0_cza = np.radians(120.7215) # latitude of HERA/PAPER
    z0_ra = np.radians(0.)

    ## sky
    """
    sky.shape = (p.nfreq, npix, 2,2)
    """
    #I,Q,U,V = [np.random.rand(p.nfreq,npix) for x in range(4)]

    if False:
        I = get_gsm_cube()
        Q,U,V = [np.zeros((p.nfreq, npix)) for x in range(3)]

    if False:
        I,Q,U,V = get_cora_polsky()
        if p.unpolarized == True:
            Q,U,V = [np.zeros((p.nfreq, npix)) for x in range(3)]

    if True:
        if (p.nside != 128) or (p.nfreq != 241): raise ValueError("The nside or nfreq of the simulation does not match the requested sky maps.")

        import h5py

        fpath = '/data4/paper/zionos/cora_maps/cora_polgalaxy1_nside128_nfreq241_band140_170.h5'
        print 'Using ' + fpath
        data = h5py.File(fpath)
        if p.unpolarized == True:
            I,_,_,_ = [data['map'][:,i,:] for i in [0,1,2,3]]
            Q,U,V = [np.zeros((p.nfreq, npix)) for x in range(3)]
        else:
            I,Q,U,V = [data['map'][:,i,:] for i in [0,1,2,3]]

    if False:
        if (p.nside != 128) or (p.nfreq != 241): raise ValueError("The nside or nfreq of the simulation does not match the requested sky maps.")

        import h5py

        fpath = '/data4/paper/zionos/cora_maps/cora_polforeground1_nside128_nfreq241_band140_170.h5'
        print 'Using ' + fpath
        data = h5py.File(fpath)
        if p.unpolarized == True:
            I,_,_,_ = [data['map'][:,i,:] for i in [0,1,2,3]]
            Q,U,V = [np.zeros((p.nfreq, npix)) for x in range(3)]
        else:
            I,Q,U,V = [data['map'][:,i,:] for i in [0,1,2,3]]

    if False:
        if (p.nside != 128) or (p.nfreq != 241): raise ValueError("The nside or nfreq of the simulation does not match the requested sky maps.")

        import h5py
        fpath = '/data4/paper/zionos/cora_maps/cora_21cm1_nside128_nfreq241_band140_170.h5'
        print 'Using ' + fpath
        data = h5py.File(fpath)

        I = data['map'][:,0,:]
        Q,U,V = [np.zeros((p.nfreq, npix)) for x in range(3)]

    I_alm, Q_alm, U_alm, V_alm = map(lambda marr: map2alm(marr, p.lmax), [I,Q,U,V])

    ## Instrument
    """
    Jdata.shape = (nfreq_in, p.npix, 2, 2)
    ijones.shape = (p.nfreq, p.npix, 2, 2)
    """
    freqs = [x * 1e6 for x in range(140,171)] # Hz
    # freqs = [(100 + 10 * x) * 1e6 for x in range(11)] # Hz. Must be converted to MHz for file list.
    #freqs = [140, 150, 160]
    tmark0 = time.time()

    nu0 = str(int(p.nu_axis[0] / 1e6))
    nuf = str(int(p.nu_axis[-1] / 1e6))
    fname = p.interp_type + "_band_" + nu0 + "-" + nuf + "mhz_nfreq" + str(p.nfreq)+ "_nside" + str(p.nside) + ".npy"

    # Ugh, this block makes baby jesus cry. l2oop
    if p.PAPER_instrument == True:
        freqs = [(100 + 10 * x) * 1e6 for x in range(11)]
        p.interp_type = 'linear' # cubic splines with this data will produce seemingly unrealistic oscillatory behavior of the beam as a function of requency
        if os.path.exists('jones_save/PAPER/' + fname) == True:
            ijones = np.load('jones_save/PAPER/' + fname)
            print "Restored Jones model"
        else:
            Jdata = irf.PAPER_instrument_setup(z0_cza)

            tmark_inst = time.time()
            print "Completed instrument_setup(), in " + str(tmark_inst - tmark0)

            ijones = interpolate_jones_freq(Jdata, freqs, interp_type=p.interp_type, save=save)

            tmark_interp = time.time()
            print "Completed interpolate_jones_freq(), in " + str(tmark_interp - tmark_inst)
    else:
        if os.path.exists('jones_save/' + fname) == True:
            ijones = np.load('jones_save/' + fname)
            print "Restored Jones model"
        else:
            Jdata = instrument_setup(z0_cza, freqs)

            tmark_inst = time.time()
            print "Completed instrument_setup(), in " + str(tmark_inst - tmark0)

            ijones = interpolate_jones_freq(Jdata, freqs, interp_type=p.interp_type, save=save)

            tmark_interp = time.time()
            print "Completed interpolate_jones_freq(), in " + str(tmark_interp - tmark_inst)

    ijonesH = np.transpose(ijones.conj(),(0,1,3,2))

    ## Baselines
    bl_eq = irf.transform_baselines(p.baselines) # get baseline vectors in equatorial coordinates

    ## For each (t,f):
    # V[t,f,0,0] == V_xx[t,f]
    # V[t,f,0,1] == V_xy[t,f]
    # V[t,f,1,0] == V_yx[t,f]
    # V[t,f,1,1] == V_yy[t,f]
    Vis = np.zeros(p.nbaseline * p.ntime * p.nfreq * 2 * 2, dtype='complex128')
    Vis = Vis.reshape(p.nbaseline, p.ntime, p.nfreq, 2, 2)

    l,m = hp.Alm.getlm(p.lmax)
    sky_list = [I_alm, Q_alm, U_alm, V_alm]
    # sky_list = [I,Q,U,V]

    tmark_loopstart = time.time()

    for b_i in range(bl_eq.shape[0]):
        ##
        """
        Fringe
        K.shape = (nfreq,npix)
        """
        c = 299792458. # meters / sec
        b = bl_eq[b_i]# meters, in the Equatorial basis
        s = hp.pix2vec(p.nside, hpxidx)
        b_dot_s = np.einsum('a...,a...',b,s)
        tau = b_dot_s / c
        K = np.exp(-2. * np.pi * 1j * np.outer(np.ones(p.nfreq), tau) * np.outer(p.nu_axis, np.ones(npix)) )

        for t in range(p.ntime):
            print "t is " + str(t)
            total_angle = 360. # degrees
            zl_ra = (float(t) / float(p.ntime)) * np.radians(total_angle)

            npix = hp.nside2npix(p.nside)

            RotAxis = np.array([0.,0.,1.])
            RotAngle = -zl_ra

            mrot = np.exp(1j * m * RotAngle)
            It, Qt, Ut, Vt = [alm2map(x * mrot, p.nside) for x in sky_list]

            sky_t = np.array([
                [It + Qt, Ut - 1j*Vt],
                [Ut + 1j*Vt, It - Qt]]).transpose(2,3,0,1) # sky_t.shape = (p.nfreq, p.npix, 2, 2)
                # Could do this iteratively! Define the differential rotation
                # and apply it in-place to the same sky tensor at each step of the time loop.

            ## Ionosphere
            """
            ionrot.shape = (p.nfreq,npix 2,2)
            """

            # RMangle = get_rm_map()
            # ion_cos = np.cos(RMangle)
            # ion_sin = np.sin(RMangle)
            ion_cos = np.ones((p.nfreq, npix))
            ion_sin = np.zeros((p.nfreq, npix))
            ion_rot = np.array([[ion_cos, ion_sin],[-ion_sin,ion_cos]])
            ion_rot = np.transpose(ion_rot,(2,3,0,1))
            ion_rotT = np.transpose(ion_rot,(0,1,3,2))
            # worried abou this...is the last line producing the right ordering,
            # or is ion_rot unchanged

            # C = np.zeros_like(sky_t)
            # irnf.jones_chain(ijones, ion_rot, sky_t, ion_rotT, ijonesH, C)
            # irnf._RIME_integral(C, K, Vis[b_i,t,:,:,:].squeeze())

            irnf.RIME_integral(ijones, ion_rot, sky_t, ion_rotT, ijonesH, K, Vis[b_i,t,:,:,:].squeeze())

    Vis /= hp.nside2npix(p.nside) # normalization
    tmark_loopstop = time.time()
    print "Visibility loop completed in " + str(tmark_loopstop - tmark_loopstart)
    print "Full run in " + str(tmark_loopstop -tmark0) + " seconds."

    out_name = "Vis_" + p.interp_type + "_band_" + str(int(p.nu_0 / 1e6)) + "-" + str(int(p.nu_f /1e6)) + "MHz_nfreq" + str(p.nfreq)+ "_ntime" + str(p.ntime) + "_nside" + str(p.nside) + ".npz"
    if p.unpolarized == True:
        out_name = "unpol" + out_name

    #if os.path.exists(out_name) == False:
    np.savez('output_vis/' + out_name, Vis=Vis, baselines=p.baselines)

class Parameters:
    pass

if __name__ == '__main__':
    #print "Note! Horizon mask is off!"
    print "Note! Ionosphere set to Identity!"
    #print "Note: Horizon mask turned off!"

    #########
    # Dimensions and Boundaries

    global p
    p = Parameters()

    p.nside = 2**7 # sets the spatial resolution of the simulation, for a given baseline

    p.lmax = 3 * p.nside - 1

    p.nfreq = 241 # the number of frequency channels at which visibilities will be computed.

    p.ntime = 288  # the number of time samples in one rotation of the earch that will be computed

    p.ndays = 1 # The number of days that will be simulated.

    p.nu_0 = 1.4e8 # Hz. The high end of the simulated frequency band.

    p.nu_f = 1.7e8 # Hz. The low end of the simulated frequency band.

    p.nu_axis = np.linspace(p.nu_0,p.nu_f,num=p.nfreq,endpoint=True)

#    p.baselines = [[15.,0,0],[0.,15.,0]]
    p.baselines = [[7.,14.,0]]

    p.nbaseline = len(p.baselines)

    p.interp_type = 'cubic'
    # options for interpolation are:
    # 'linear' and 'cubic', both via scipy.interpolate.interp1d()

    p.PAPER_instrument = False # hack hack hack

    p.unpolarized = True

    ## OLD OPTIONS
    #   'linear' : linear interpolation between nodes
    #   'hermite': Piecewise Cubic Hermite Interpolating Polynomials between each
    #       pair of nodes. This produces a monotonic interpolant between each pair
    #       of nodes, but the derivative is not continuous at the nodes i.e there
    #       are corners in the interpolant. This one takes the longest to compute, ~6.5x 'linear'.
    #   'fitspline': cubic spline fit to the nodes. Does NOT interpolate the nodes.
    #   'spline': interpolating cubic spline

    global debug
    debug = False

    if p.unpolarized == True:
        print "Polarization turned off"

    main(p,save=True)
    print "Compiled successfully"
